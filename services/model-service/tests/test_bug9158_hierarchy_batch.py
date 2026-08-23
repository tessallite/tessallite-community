"""Bug-9158: hierarchy model-open metadata is a bounded batch contract.

Test escape: the old with-levels route reopened levels, attributes, columns and
UDAs once per hierarchy/level.  Serialization coverage could stay green while
the operational database still received a fan-out.  This guard supplies two
hierarchies with physical and UDA attributes, exercises persona filtering, and
asserts the fixed model-scoped query budget.

Guard: ``test_l13_r1_f1_hierarchy_with_levels_has_constant_query_count``.
Tier: T2.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.api.hierarchies import list_hierarchies_with_levels

from .conftest import TEST_MODEL_ID, async_gen_from
from .result_fakes import FakeResult

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_l13_r1_f1_hierarchy_with_levels_has_constant_query_count() -> None:
    now = datetime.now(timezone.utc)
    hierarchy_one_id, hierarchy_two_id = uuid.uuid4(), uuid.uuid4()
    level_one_id, level_two_id, level_three_id = (uuid.uuid4() for _ in range(3))
    country_column_id, city_column_id = uuid.uuid4(), uuid.uuid4()
    uda_id = uuid.uuid4()
    table_id, city_table_id = uuid.uuid4(), uuid.uuid4()

    def hierarchy(hierarchy_id, name):
        return types.SimpleNamespace(
            id=hierarchy_id,
            model_id=TEST_MODEL_ID,
            name=name,
            type="explicit",
            dimension_kind="geo",
            description=None,
            segment_config=None,
            date_config=None,
            calendar_type=None,
            fiscal_year_start_month=None,
            created_at=now,
            updated_at=now,
        )

    hierarchies = [
        hierarchy(hierarchy_one_id, "Geography"),
        hierarchy(hierarchy_two_id, "Customer geography"),
    ]
    levels = [
        types.SimpleNamespace(
            id=level_one_id, hierarchy_id=hierarchy_one_id, name="Country",
            ordinal=0, key_attribute_id=country_column_id,
            key_attribute_source="physical_column", description=None,
            time_unit=None, allowed_time_calcs=[],
        ),
        types.SimpleNamespace(
            id=level_two_id, hierarchy_id=hierarchy_one_id, name="City",
            ordinal=1, key_attribute_id=city_column_id,
            key_attribute_source="physical_column", description=None,
            time_unit=None, allowed_time_calcs=[],
        ),
        types.SimpleNamespace(
            id=level_three_id, hierarchy_id=hierarchy_two_id, name="Segment",
            ordinal=0, key_attribute_id=uda_id,
            key_attribute_source="user_defined_attribute", description=None,
            time_unit=None, allowed_time_calcs=[],
        ),
    ]
    level_attributes = [
        types.SimpleNamespace(
            id=uuid.uuid4(), level_id=level_one_id,
            attribute_id=city_column_id, attribute_source="physical_column",
            role="display",
        ),
        types.SimpleNamespace(
            id=uuid.uuid4(), level_id=level_three_id,
            attribute_id=country_column_id, attribute_source="physical_column",
            role="filter",
        ),
    ]
    tables = [
        types.SimpleNamespace(
            id=table_id, model_id=TEST_MODEL_ID, alias="customers",
            display_name="Customers", physical_name="public.customers",
            table_type="dim_detail",
        ),
        types.SimpleNamespace(
            id=city_table_id, model_id=TEST_MODEL_ID, alias="cities",
            display_name="Cities", physical_name="public.cities",
            table_type="dim_detail",
        ),
    ]
    columns = [
        types.SimpleNamespace(
            id=country_column_id, model_table_id=table_id,
            column_name="country", data_type="text",
        ),
        types.SimpleNamespace(
            id=city_column_id, model_table_id=city_table_id,
            column_name="city", data_type="text",
        ),
    ]
    udas = [
        types.SimpleNamespace(
            id=uda_id, model_id=TEST_MODEL_ID, table_id=table_id,
            name="customer_segment", output_data_type="string",
        ),
    ]
    responses = iter([
        FakeResult(hierarchies),
        FakeResult(levels),
        FakeResult(level_attributes),
        FakeResult(columns),
        FakeResult(udas),
        FakeResult(tables),
    ])
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=lambda _statement: next(responses))
    persona = types.SimpleNamespace(included_hierarchy_ids=None)
    current_user = types.SimpleNamespace(tenant_id="tenant-1")

    with (
        patch("src.api.hierarchies.enforce_model_scope"),
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)),
        patch("src.api.hierarchies._ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies.get_excluded_level_attribute_ids",
            new=AsyncMock(return_value={city_column_id}),
        ),
    ):
        result = await list_hierarchies_with_levels(
            project_id=uuid.uuid4(),
            model_id=TEST_MODEL_ID,
            persona_id=uuid.uuid4(),
            current_user=current_user,
        )

    assert [item.name for item in result] == ["Geography", "Customer geography"]
    assert [level.name for level in result[0].levels] == ["Country"]
    assert result[0].levels[0].key_attribute.name == "country"
    assert result[0].levels[0].attributes[0].attribute.name == "city"
    assert result[1].levels[0].key_attribute.name == "customer_segment"
    assert result[1].levels[0].key_attribute.source == "user_defined_attribute"
    # One hierarchy query plus levels, level attributes, physical columns, UDAs
    # and tables.  It is constant for this model-open route, not per hierarchy.
    assert db.execute.await_count == 6
    db.get.assert_not_awaited()
