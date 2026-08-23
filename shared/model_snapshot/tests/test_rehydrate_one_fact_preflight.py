"""F-013-09 / F-020-12 (Bug-9120) — one-fact preflight is centralized.

The one-fact-per-model cap used to be preflighted only by the project-import and
model-JSON-import consumers. Every OTHER ``rehydrate_into_live`` caller (revert,
catalogue / dbt / cube / atscale / YAML import) reached the per-row INSERT and
tripped the partial unique index as a raw IntegrityError / 500. Centralizing the
check at the START of ``rehydrate_into_live`` gives every caller the same typed
``OneFactViolationError`` (subclass of ``SnapshotSchemaError``) BEFORE any row is
touched — proven here by passing ``tenant_db=None``: a two-fact snapshot must
raise before the DB is ever consulted.
"""
from __future__ import annotations

import uuid

import pytest

from shared.model_snapshot.rehydrator import (
    OneFactViolationError,
    SnapshotSchemaError,
    rehydrate_into_live,
)


def _table(table_type, name):
    return {"id": str(uuid.uuid4()), "table_type": table_type, "physical_name": name}


def _snapshot(tables):
    return {"schema_version": 5, "tables": tables}


@pytest.mark.asyncio
async def test_two_fact_tables_raise_before_db():
    snap = _snapshot([_table("fact", "sales"), _table("fact", "orders")])
    # tenant_db=None proves the preflight raises before any DB access.
    with pytest.raises(OneFactViolationError) as ei:
        await rehydrate_into_live(uuid.uuid4(), snap, None)
    assert "exactly one fact table" in str(ei.value)
    assert "sales" in str(ei.value) and "orders" in str(ei.value)


@pytest.mark.asyncio
async def test_one_fact_violation_is_a_snapshot_schema_error():
    # Callers that catch SnapshotSchemaError must still catch the one-fact case.
    snap = _snapshot([_table("fact", "a"), _table("fact", "b")])
    with pytest.raises(SnapshotSchemaError):
        await rehydrate_into_live(uuid.uuid4(), snap, None)


@pytest.mark.asyncio
async def test_single_fact_snapshot_passes_preflight():
    # One fact + one dim must clear the preflight; it then reaches the model
    # load (tenant_db=None -> AttributeError), proving the preflight did NOT
    # raise OneFactViolationError for a valid one-fact bundle.
    snap = _snapshot([_table("fact", "sales"), _table("dim_detail", "customer")])
    with pytest.raises(Exception) as ei:
        await rehydrate_into_live(uuid.uuid4(), snap, None)
    assert not isinstance(ei.value, OneFactViolationError)


@pytest.mark.asyncio
async def test_bug_8614_multi_table_zero_fact_snapshot_fails_before_db():
    snap = _snapshot([
        _table("dim_detail", "customers"),
        _table("dim_detail", "regions"),
    ])
    with pytest.raises(OneFactViolationError, match="exactly one fact table"):
        await rehydrate_into_live(uuid.uuid4(), snap, None)
