"""Tests for table auto-analysis endpoint and heuristic logic (T-6)."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from shared.semantic.table_analyzer import analyze_table, TableAnalysisResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_col(name: str, dtype: str, col_id: uuid.UUID | None = None):
    return types.SimpleNamespace(
        id=col_id or uuid.uuid4(),
        column_name=name,
        data_type=dtype,
        is_nullable=True,
        is_hidden=False,
        display_name=None,
        description=None,
        cardinality_estimate=None,
        last_stats_at=None,
    )


def _make_table(
    table_type: str = "fact",
    columns: list | None = None,
    table_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=table_id or uuid.uuid4(),
        table_type=table_type,
        columns=columns or [],
    )


def _override_user():
    return CurrentUser(
        user_id="admin@test.com",
        tenant_id="t1",
        email="admin@test.com",
        role="tenant_admin",
    )


# ---------------------------------------------------------------------------
# Unit tests — heuristic logic
# ---------------------------------------------------------------------------

def test_analyze_fact_table_classifies_measures_and_dates():
    """A fact table's columns are classified as measures + date_key correctly."""
    cols = [
        _make_col("order_id", "integer"),
        _make_col("revenue_amount", "numeric"),
        _make_col("quantity", "integer"),
        _make_col("order_date", "date"),
        _make_col("customer_id", "integer"),
    ]
    table = _make_table(table_type="fact", columns=cols)
    result = analyze_table(table)
    assert result.suggested_table_type == "fact"
    assert result.confidence == "high"
    assert result.potential_calendar_column == "order_date"
    roles = {s.column_name: s.suggested_role for s in result.column_suggestions}
    assert roles["revenue_amount"] == "measure"
    assert roles["quantity"] == "measure"
    assert roles["order_date"] == "date_key"
    assert roles["order_id"] == "dimension"
    assert roles["customer_id"] == "dimension"


def test_analyze_dim_table_no_measures():
    """A table with only FK/name columns and no measures suggests dim_detail."""
    cols = [
        _make_col("customer_id", "integer"),
        _make_col("customer_name", "varchar"),
        _make_col("region_code", "varchar"),
        _make_col("segment", "varchar"),
    ]
    table = _make_table(table_type="dim_detail", columns=cols)
    result = analyze_table(table)
    assert result.suggested_table_type == "dim_detail"


def test_analyze_column_suggestions_classify_roles():
    """Column suggestions should tag date columns as date_key and measures as measure."""
    cols = [
        _make_col("created_at", "timestamp"),
        _make_col("total_sales", "numeric"),
        _make_col("region_id", "integer"),
    ]
    table = _make_table(table_type="fact", columns=cols)
    result = analyze_table(table)
    roles = {s.column_name: s.suggested_role for s in result.column_suggestions}
    assert roles["created_at"] == "date_key"
    assert roles["total_sales"] == "measure"
    assert roles["region_id"] == "dimension"


def test_analyze_empty_table_returns_result_without_crash():
    """An empty table (no columns) should not raise and should return low confidence."""
    table = _make_table(table_type="dim_detail", columns=[])
    result = analyze_table(table)
    assert isinstance(result, TableAnalysisResult)
    assert result.confidence == "low"
    assert result.column_suggestions == []


# ---------------------------------------------------------------------------
# API endpoint test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_endpoint_returns_200():
    """POST /tables/{table_id}/analyze should return 200 with suggestion fields."""
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()

    cols = [
        _make_col("sale_amount", "numeric"),
        _make_col("sale_date", "date"),
        _make_col("product_id", "integer"),
    ]
    table = types.SimpleNamespace(
        id=table_id,
        model_id=model_id,
        source_id=source_id,
        table_type="fact",
        columns=cols,
    )

    db = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = table
    db.execute = AsyncMock(return_value=scalar_result)

    async def _get(entity, pk):
        # Bug-8862: the analyze route now proves project -> model before it
        # loads the table.
        if entity.__name__ == "Model":
            return types.SimpleNamespace(id=pk, project_id=project_id)
        return None

    db.get = AsyncMock(side_effect=_get)

    async def _fake_db(*args, **kwargs):
        yield db

    app.dependency_overrides[get_current_user] = _override_user

    with patch("src.api.tables.get_tenant_db", _fake_db):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/projects/{project_id}/models/{model_id}"
                f"/sources/{source_id}/tables/{table_id}/analyze"
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["suggested_table_type"] == "fact"
    assert "column_suggestions" in data
    assert "date_columns" in data

    app.dependency_overrides.pop(get_current_user, None)
