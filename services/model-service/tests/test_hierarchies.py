"""
Unit tests for hierarchy helper behavior.
"""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock

from src.api.hierarchies import (
    _date_component_expression,
    _normalize_hierarchy_type,
    _resolve_attribute,
    _slugify_name,
    _types_compatible,
)

pytestmark = pytest.mark.unit


def test_normalize_hierarchy_type_accepts_supported_values():
    assert _normalize_hierarchy_type("explicit") == "explicit"
    assert _normalize_hierarchy_type("date_embedded") == "date_embedded"
    assert _normalize_hierarchy_type("segment") == "segment"


def test_normalize_hierarchy_type_rejects_unsupported_values():
    with pytest.raises(HTTPException) as exc:
        _normalize_hierarchy_type("unsupported")
    assert exc.value.status_code == 422


def test_types_compatible_numeric_family():
    assert _types_compatible("integer", "numeric") is True
    assert _types_compatible("bigint", "double precision") is True


def test_types_compatible_rejects_incompatible_families():
    assert _types_compatible("varchar", "integer") is False
    assert _types_compatible("date", "varchar") is False


def test_slugify_name_normalizes_special_characters():
    assert _slugify_name("Payment Date") == "payment_date"
    assert _slugify_name(" account-code ") == "account_code"


def test_date_component_expression_generates_sql():
    assert _date_component_expression("payment_date", "year") == "EXTRACT(YEAR FROM (payment_date))"
    assert _date_component_expression("payment_date", "half_year").startswith("CASE WHEN EXTRACT(MONTH FROM")
    assert _date_component_expression("payment_date", "day") == "CAST((payment_date) AS DATE)"


def test_date_component_expression_rejects_unknown_component():
    with pytest.raises(HTTPException) as exc:
        _date_component_expression("payment_date", "unknown")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_resolve_attribute_allows_fact_table_when_not_required():
    model_id = uuid.uuid4()
    table_id = uuid.uuid4()
    col_id = uuid.uuid4()

    fact_table = types.SimpleNamespace(
        id=table_id,
        model_id=model_id,
        table_type="fact",
        alias="fact_txn",
        display_name="Fact Txn",
        physical_name="fact_txn",
    )
    column = types.SimpleNamespace(
        id=col_id,
        model_table_id=table_id,
        data_type="date",
        column_name="transaction_date",
    )

    async def _fake_get(entity, entity_id):
        name = getattr(entity, "__name__", "")
        if name == "ModelColumn" and entity_id == col_id:
            return column
        if name == "ModelTable" and entity_id == table_id:
            return fact_table
        return None

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(side_effect=_fake_get)

    resolved = await _resolve_attribute(
        mock_db,
        model_id=model_id,
        attribute_id=col_id,
        source="physical_column",
        require_dimension_table=False,
    )

    assert resolved.ref.table_id == table_id
    assert resolved.ref.name == "transaction_date"
    assert resolved.table.table_type == "fact"
