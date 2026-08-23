"""Bug-8768 API producer contract for aggregate incremental authority."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from shared.schemas.pydantic_models import RefreshPolicyCreate, RefreshPolicyUpdate
from shared.db.models import Model
from src.api.refresh import upsert_refresh_policy


@pytest.mark.asyncio
async def test_upsert_round_trips_append_only_and_periodic_full_rebuild_fields():
    agg_id = uuid.uuid4()
    policy = types.SimpleNamespace(
        id=uuid.uuid4(),
        aggregate_definition_id=agg_id,
        refresh_mode="scheduled",
        cron_expression="0 2 * * *",
        incremental_column=None,
        incremental_lookback=None,
        incremental_append_only=False,
        full_rebuild_interval_days=None,
        is_enabled=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = policy
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # Bug-8786: the handler now proves project -> model -> aggregate before it
    # touches the policy, so the fixture must supply a correctly-scoped chain.
    # The assertions below are unchanged: this is the append-only round-trip
    # contract, not an authorization test (that is test_refresh_policy_scoping).
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()

    async def _db_get(entity, entity_id):
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=project_id)
        return types.SimpleNamespace(
            id=entity_id, model_id=model_id, physical_table_name="agg_t"
        )

    db.get = AsyncMock(side_effect=_db_get)

    async def _tenant_db(_tenant_id):
        yield db

    body = RefreshPolicyCreate(
        refresh_mode="incremental",
        cron_expression="0 2 * * *",
        incremental_column="business_date",
        incremental_lookback=7,
        incremental_append_only=True,
        full_rebuild_interval_days=30,
    )

    with patch("src.api.refresh.get_tenant_db", new=_tenant_db):
        response = await upsert_refresh_policy(
            project_id,
            model_id,
            agg_id,
            body,
            current_user=types.SimpleNamespace(tenant_id="acme"),
        )

    assert response.refresh_mode == "incremental"
    assert response.incremental_append_only is True
    assert response.full_rebuild_interval_days == 30
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("value", ["true", "false", "1", 1, 0])
def test_create_schema_rejects_non_boolean_append_only_values(value):
    """Bug-8768/R3: JSON coercion must not create append-only authority."""
    with pytest.raises(ValidationError):
        RefreshPolicyCreate(
            refresh_mode="incremental",
            cron_expression="0 2 * * *",
            incremental_column="business_date",
            incremental_append_only=value,
        )


@pytest.mark.parametrize("value", ["true", "false", "1", 1, 0])
def test_update_schema_rejects_non_boolean_append_only_values(value):
    """Partial updates must enforce the same strict write-boundary contract."""
    with pytest.raises(ValidationError):
        RefreshPolicyUpdate(incremental_append_only=value)


def test_omitted_append_only_fields_keep_legacy_defaults():
    create = RefreshPolicyCreate(
        refresh_mode="scheduled",
        cron_expression="0 2 * * *",
        is_enabled=True,
    )
    assert create.incremental_append_only is False
    assert RefreshPolicyUpdate().incremental_append_only is None
