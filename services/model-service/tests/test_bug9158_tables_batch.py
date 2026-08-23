"""Bug-9158: model-open table metadata is a bounded batch contract.

Test escape: the pre-fix Builder opened one model-table catalogue per source
and then one attribute route per rendered table.  A green route serialization
test alone would not prove the fan-out was removed, so this guard calls the
model-scoped endpoint and asserts its single result contains both table
metadata and the complete physical/UDA attribute payload.

Guard: ``test_model_open_batch_returns_tables_and_attributes_in_one_route``.
Tier: T2.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.schemas.pydantic_models import TableAttributeResponse
from src.api.tables import list_tables_with_attributes

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from
from .result_fakes import FakeResult

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_model_open_batch_returns_tables_and_attributes_in_one_route() -> None:
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    uda_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    table = types.SimpleNamespace(
        id=table_id,
        model_id=TEST_MODEL_ID,
        source_id=uuid.uuid4(),
        table_type="dim_detail",
        physical_name="public.customers",
        alias="customers",
        display_name="Customers",
        description=None,
        row_count_estimate=None,
        last_stats_at=None,
        calendar_table_id=None,
        created_at=now,
        updated_at=now,
        columns=[types.SimpleNamespace(
            id=column_id,
            column_name="customer_id",
            display_name="Customer ID",
            description="Stable customer key",
            is_hidden=False,
            hidden_reason=None,
            is_primary_key=True,
            data_type="uuid",
        )],
        user_defined_attributes=[types.SimpleNamespace(
            id=uda_id,
            name="customer_segment",
            description="Derived segment",
            output_data_type="string",
            is_generated=False,
            expression="segment(customer_id)",
            validated=True,
            validation_error=None,
        )],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=FakeResult([table]))

    with (
        patch("src.api.tables.get_tenant_db", async_gen_from(db)),
        patch("src.api.tables.ensure_model_in_project", new=AsyncMock()),
    ):
        rows = await list_tables_with_attributes(
            TEST_PROJECT_ID,
            TEST_MODEL_ID,
            current_user=types.SimpleNamespace(tenant_id="tenant-1"),
        )

    assert len(rows) == 1
    assert rows[0].table.id == table_id
    assert [attribute.name for attribute in rows[0].attributes] == [
        "customer_id",
        "customer_segment",
    ]
    assert rows[0].attributes[0] == TableAttributeResponse(
        kind="physical",
        id=column_id,
        table_id=table_id,
        name="customer_id",
        display_name="Customer ID",
        description="Stable customer key",
        is_hidden=False,
        hidden_reason=None,
        is_primary_key=True,
        data_type="uuid",
        is_user_defined=False,
        validated=None,
        validation_error=None,
    )
    # The endpoint executes one model-scoped catalogue statement.  SQLAlchemy's
    # select-in loader may issue bounded relationship queries in production,
    # but the client-facing request count is independent of table count.
    db.execute.assert_awaited_once()
