"""Tests for hierarchy health monitor (Block D)."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_health_check_empty_levels_is_error():
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "explicit"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []

    join_result = MagicMock()
    join_result.all.return_value = []

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = None

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    issues = await _check_hierarchy_health(db, hier)
    assert any(i["issue_type"] == "empty_levels" for i in issues)


@pytest.mark.asyncio
async def test_health_check_valid_hierarchy_no_issues():
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "date_embedded"
    hier.date_config = {"source_attribute_id": str(uuid4())}
    level = MagicMock()
    level.ordinal = 1
    level.name = "Year"
    level.key_attribute_id = uuid4()
    level.key_attribute_source = "physical_column"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = [level]

    table_id = uuid4()
    fact_id = uuid4()
    join_result = MagicMock()
    join_result.all.return_value = [(fact_id, table_id)]

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    col_mock = MagicMock()
    col_mock.model_table_id = table_id
    db.get = AsyncMock(return_value=col_mock)
    issues = await _check_hierarchy_health(db, hier)
    assert issues == []


@pytest.mark.asyncio
async def test_get_model_health_returns_per_hierarchy_status():
    from src.api.hierarchy_health import _get_model_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.name = "Date Hierarchy"
    hier.type = "explicit"

    hier_result = MagicMock()
    hier_result.scalars.return_value.all.return_value = [hier]

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []

    join_result = MagicMock()
    join_result.all.return_value = []

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = None

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[hier_result, levels_result, join_result, fact_result, links_result])

    health = await _get_model_hierarchy_health(db, model_id=uuid4())
    assert len(health) == 1
    assert health[0]["status"] == "error"


@pytest.mark.asyncio
async def test_unreachable_table_in_disconnected_join_component():
    """A table in a disconnected component (not reachable from fact) is flagged."""
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "explicit"

    fact_id = uuid4()
    dim_connected = uuid4()
    dim_disconnected = uuid4()
    dim_other = uuid4()

    level = MagicMock()
    level.ordinal = 1
    level.name = "Disconnected Level"
    level.key_attribute_id = uuid4()
    level.key_attribute_source = "physical_column"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = [level]

    # Join graph: fact--dim_connected, dim_disconnected--dim_other (disconnected)
    join_result = MagicMock()
    join_result.all.return_value = [
        (fact_id, dim_connected),
        (dim_disconnected, dim_other),
    ]

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    col_mock = MagicMock()
    col_mock.model_table_id = dim_disconnected
    db.get = AsyncMock(return_value=col_mock)

    issues = await _check_hierarchy_health(db, hier)
    assert any(i["issue_type"] == "unreachable_level_table" for i in issues)


@pytest.mark.asyncio
async def test_level_on_fact_table_is_reachable():
    """A level on the fact table itself should not be flagged as unreachable."""
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "explicit"

    fact_id = uuid4()

    level = MagicMock()
    level.ordinal = 1
    level.name = "Fact Level"
    level.key_attribute_id = uuid4()
    level.key_attribute_source = "physical_column"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = [level]

    # No joins at all
    join_result = MagicMock()
    join_result.all.return_value = []

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    col_mock = MagicMock()
    col_mock.model_table_id = fact_id
    db.get = AsyncMock(return_value=col_mock)

    issues = await _check_hierarchy_health(db, hier)
    assert not any(i["issue_type"] == "unreachable_level_table" for i in issues)
