"""F-009-18 — DataTarget connection validation at write time.

``_validate_target_connection`` rejects a target whose referenced connection
is missing, belongs to a different project, or is an unsupported connector,
so the optimizer/scheduler never surface those errors later as a confusing
source-side SQL syntax error.
"""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from src.api.targets import _validate_target_connection


def _model(project_id):
    return types.SimpleNamespace(id=uuid.uuid4(), project_id=project_id)


def _conn(project_id, connection_type="postgresql"):
    return types.SimpleNamespace(project_id=project_id, connection_type=connection_type)


class _DB:
    def __init__(self, model=None, conn=None):
        self._model = model
        self._conn = conn

    async def get(self, cls, _id):
        from shared.db.models import Model, ProjectConnection
        if cls is Model:
            return self._model
        if cls is ProjectConnection:
            return self._conn
        return None


@pytest.mark.asyncio
async def test_valid_connection_passes():
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(pid, "postgresql"))
    # No exception == pass.
    await _validate_target_connection(db, pid, model.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_missing_connection_rejected():
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=None)
    with pytest.raises(HTTPException) as exc:
        await _validate_target_connection(db, pid, model.id, uuid.uuid4())
    assert exc.value.status_code == 422
    assert "existing connection" in exc.value.detail


@pytest.mark.asyncio
async def test_cross_project_connection_rejected():
    pid = uuid.uuid4()
    other = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(other, "postgresql"))
    with pytest.raises(HTTPException) as exc:
        await _validate_target_connection(db, pid, model.id, uuid.uuid4())
    assert exc.value.status_code == 422
    assert "different project" in exc.value.detail


@pytest.mark.asyncio
async def test_unsupported_connector_rejected():
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(pid, "mongodb"))
    with pytest.raises(HTTPException) as exc:
        await _validate_target_connection(db, pid, model.id, uuid.uuid4())
    assert exc.value.status_code == 422
    assert "not a supported aggregate target" in exc.value.detail


@pytest.mark.asyncio
async def test_legacy_jdbc_alias_accepted():
    """jdbc normalises to hadoop_spark, a supported target."""
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(pid, "jdbc"))
    await _validate_target_connection(db, pid, model.id, uuid.uuid4())
