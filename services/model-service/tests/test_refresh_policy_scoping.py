"""Bug-8786: refresh routes must bind project -> model -> aggregate.

Test escape: every handler in ``src/api/refresh.py`` declared ``project_id`` and
``model_id`` as path parameters and then queried by ``agg_id`` alone. RBAC
(``require_role``) reads ``project_id`` from the path and checks the CALLER'S
binding for that project; it never proves the nested resource belongs to it. So
a caller with a legitimate binding in project A could pass project_id=A to
satisfy RBAC and a model/aggregate from project B, and read or mutate B.
Nothing asserted that a nested route rejects a foreign resource.

Guard: these tests. Tier: T1.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared.db.models import AggregateDefinition, Model
from shared.schemas.pydantic_models import RefreshPolicyCreate
from src.api.refresh import (
    get_refresh_policy,
    list_model_refresh_runs,
    list_refresh_runs,
    upsert_refresh_policy,
)

TENANT = types.SimpleNamespace(tenant_id="acme")


def _db(*, model_project_id, agg_model_id):
    """A db whose Model/AggregateDefinition belong to the given owners."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    result.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _get(entity, entity_id):
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=model_project_id)
        if entity is AggregateDefinition:
            return types.SimpleNamespace(
                id=entity_id, model_id=agg_model_id, physical_table_name="agg_t"
            )
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _patch(db):
    async def _tenant_db(_tenant_id):
        yield db

    return patch("src.api.refresh.get_tenant_db", new=_tenant_db)


BODY = RefreshPolicyCreate(
    refresh_mode="incremental",
    cron_expression="0 2 * * *",
    incremental_column="business_date",
    incremental_append_only=True,
)


@pytest.mark.asyncio
async def test_upsert_policy_rejects_model_from_another_project():
    """The write path is the one that matters most: no cross-project mutation."""
    caller_project, other_project = uuid.uuid4(), uuid.uuid4()
    model_id = uuid.uuid4()
    db = _db(model_project_id=other_project, agg_model_id=model_id)

    with _patch(db), pytest.raises(HTTPException) as exc:
        await upsert_refresh_policy(
            caller_project, model_id, uuid.uuid4(), BODY, current_user=TENANT
        )
    assert exc.value.status_code == 404
    assert exc.value.detail in {"Model not found", "Aggregate not found"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_policy_rejects_aggregate_from_another_model():
    caller_project = uuid.uuid4()
    db = _db(model_project_id=caller_project, agg_model_id=uuid.uuid4())

    with _patch(db), pytest.raises(HTTPException) as exc:
        await upsert_refresh_policy(
            caller_project, uuid.uuid4(), uuid.uuid4(), BODY, current_user=TENANT
        )
    assert exc.value.status_code == 404
    assert exc.value.detail in {"Model not found", "Aggregate not found"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_policy_rejects_aggregate_from_another_model():
    caller_project = uuid.uuid4()
    db = _db(model_project_id=caller_project, agg_model_id=uuid.uuid4())

    with _patch(db), pytest.raises(HTTPException) as exc:
        await get_refresh_policy(
            caller_project, uuid.uuid4(), uuid.uuid4(), current_user=TENANT
        )
    assert exc.value.status_code == 404
    assert exc.value.detail in {"Model not found", "Aggregate not found"}


@pytest.mark.asyncio
async def test_run_history_rejects_aggregate_from_another_model():
    caller_project = uuid.uuid4()
    db = _db(model_project_id=caller_project, agg_model_id=uuid.uuid4())

    with _patch(db), pytest.raises(HTTPException) as exc:
        await list_refresh_runs(
            caller_project, uuid.uuid4(), uuid.uuid4(), current_user=TENANT
        )
    assert exc.value.status_code == 404
    assert exc.value.detail in {"Model not found", "Aggregate not found"}


@pytest.mark.asyncio
async def test_model_run_history_rejects_model_from_another_project():
    """The model-level route filtered by model_id but never proved the project."""
    caller_project = uuid.uuid4()
    db = _db(model_project_id=uuid.uuid4(), agg_model_id=uuid.uuid4())

    with _patch(db), pytest.raises(HTTPException) as exc:
        await list_model_refresh_runs(caller_project, uuid.uuid4(), current_user=TENANT)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Model not found"


@pytest.mark.asyncio
async def test_correctly_scoped_request_is_allowed():
    """The guard must not be a blanket denial — a valid chain still works."""
    project_id, model_id = uuid.uuid4(), uuid.uuid4()
    db = _db(model_project_id=project_id, agg_model_id=model_id)

    with _patch(db):
        runs = await list_refresh_runs(
            project_id, model_id, uuid.uuid4(), current_user=TENANT
        )
    assert runs == []
