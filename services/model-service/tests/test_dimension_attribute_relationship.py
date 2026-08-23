"""Unit tests for the dimension attribute relationship resolution helpers.

Spec: architecture_derived-grain-aggregate-routing.md §5.3 / §7.6.1. These pin two
correctness contracts the review flagged:

  1. key_column_id is PINNED — resolving without an explicit key uses the
     dimension's current physical key, and (in the update path) a detail/cardinality
     edit MUST NOT re-pin the key from the live dimension.
  2. columns must ALREADY EXIST — a relationship declaration may not fabricate a
     phantom column (unlike the shared resolve_column create-if-missing helper).
"""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from src.api import dimensions as dim_api

pytestmark = pytest.mark.unit


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Async DB stub: ``execute(select(ModelColumn)...)`` returns the column
    registered for a (table_id, name) pair, else None; ``get`` returns a column
    by id."""

    def __init__(self, cols_by_key=None, cols_by_id=None):
        self._by_key = cols_by_key or {}
        self._by_id = cols_by_id or {}

    async def execute(self, stmt):
        # Extract the (model_table_id, column_name) equality values from the
        # compiled WHERE params. Robust enough for these focused tests.
        params = stmt.compile().params
        table_id = None
        name = None
        for v in params.values():
            if isinstance(v, uuid.UUID):
                table_id = v
            elif isinstance(v, str):
                name = v
        return _FakeResult(self._by_key.get((table_id, name)))

    async def get(self, _model, key):
        return self._by_id.get(key)


def _col(col_id, table_id, name):
    return types.SimpleNamespace(id=col_id, model_table_id=table_id, column_name=name)


def _dim(source_column_id):
    return types.SimpleNamespace(id=uuid.uuid4(), source_column_id=source_column_id)


@pytest.fixture(autouse=True)
def _patch_source_table(monkeypatch):
    """_source_table_id_for_dim needs the DB; stub it to a fixed table id."""
    table_id = uuid.uuid4()

    async def _fake_table(_db, _dim):
        return table_id

    monkeypatch.setattr(dim_api, "_source_table_id_for_dim", _fake_table)
    return table_id


@pytest.mark.asyncio
async def test_lookup_existing_column_rejects_missing_column(_patch_source_table):
    table_id = _patch_source_table
    db = _FakeDB(cols_by_key={})  # nothing registered
    with pytest.raises(HTTPException) as exc:
        await dim_api._lookup_existing_column(db, table_id, "ghost_col")
    assert exc.value.status_code == 422
    assert "does not exist" in exc.value.detail


@pytest.mark.asyncio
async def test_resolve_key_defaults_to_dimension_key_when_not_supplied(_patch_source_table):
    key_id = uuid.uuid4()
    dim = _dim(source_column_id=key_id)
    db = _FakeDB()
    out = await dim_api._resolve_relationship_key(db, dim=dim, key_column_name=None)
    assert out == key_id


@pytest.mark.asyncio
async def test_resolve_key_uses_explicit_existing_column(_patch_source_table):
    table_id = _patch_source_table
    key_id = uuid.uuid4()
    explicit_id = uuid.uuid4()
    dim = _dim(source_column_id=key_id)
    db = _FakeDB(cols_by_key={(table_id, "biz_key"): _col(explicit_id, table_id, "biz_key")})
    out = await dim_api._resolve_relationship_key(db, dim=dim, key_column_name="biz_key")
    assert out == explicit_id


@pytest.mark.asyncio
async def test_resolve_detail_rejects_equal_to_key(_patch_source_table):
    table_id = _patch_source_table
    key_id = uuid.uuid4()
    dim = _dim(source_column_id=key_id)
    db = _FakeDB(cols_by_key={(table_id, "same"): _col(key_id, table_id, "same")})
    with pytest.raises(HTTPException) as exc:
        await dim_api._resolve_relationship_detail(
            db, dim=dim, detail_column_name="same", key_column_id=key_id
        )
    assert exc.value.status_code == 422
    assert "differ" in exc.value.detail


@pytest.mark.asyncio
async def test_resolve_detail_returns_existing_distinct_column(_patch_source_table):
    table_id = _patch_source_table
    key_id = uuid.uuid4()
    detail_id = uuid.uuid4()
    dim = _dim(source_column_id=key_id)
    db = _FakeDB(cols_by_key={(table_id, "name"): _col(detail_id, table_id, "name")})
    out = await dim_api._resolve_relationship_detail(
        db, dim=dim, detail_column_name="name", key_column_id=key_id
    )
    assert out == detail_id
