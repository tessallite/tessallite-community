"""Tests for Block D — Date Hierarchy Batch-Create."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from src.api.hierarchies import _type_family, DATE_HIERARCHY_TEMPLATES
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    async_gen_from,
    make_mock_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model():
    return types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)


def _make_cal_mt(cal_table_id: uuid.UUID):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        source_id=uuid.uuid4(),
        table_type="dim_detail",
        physical_name="dim_date",
        alias="dim_date",
        display_name="Date Dimension",
        calendar_table_id=cal_table_id,
    )


def _make_cal_info(cal_table_id: uuid.UUID):
    return types.SimpleNamespace(
        id=cal_table_id,
        date_column="date_key",
    )


def _make_date_key_col(table_id: uuid.UUID):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_table_id=table_id,
        column_name="date_key",
        display_name="Date Key",
        data_type="date",
        is_nullable=False,
    )


def _make_col(col_name: str, data_type: str = "date", table_id: uuid.UUID | None = None):
    from src.api.hierarchies import _UnassignedDateAttr
    tbl_id = table_id or uuid.uuid4()
    return _UnassignedDateAttr(
        id=uuid.uuid4(),
        column_name=col_name,
        display_name=col_name,
        data_type=data_type,
        table_id=tbl_id,
        table_alias="fact_sales",
        is_uda=False,
        physical_column_id=None,
    )


@pytest.fixture
def auth_modeler():
    user = CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def modeler_client(auth_modeler):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# D.1 — _type_family unit tests
# ---------------------------------------------------------------------------

def test_type_family_datetime_types():
    """date, timestamp, and time subtypes are classified as datetime."""
    assert _type_family("date") == "datetime"
    assert _type_family("timestamp") == "datetime"
    assert _type_family("timestamp with time zone") == "datetime"
    assert _type_family("datetime") == "datetime"


def test_type_family_non_date_types():
    """varchar, integer, and numeric are not datetime."""
    assert _type_family("varchar") != "datetime"
    assert _type_family("integer") != "datetime"
    assert _type_family("numeric") != "datetime"


def test_date_hierarchy_templates_defined():
    """All expected grain templates are present."""
    for grain in ("y_m_d", "y_q_m_d", "y_h_q_m_d", "y_w_d", "y_m_w_d"):
        assert grain in DATE_HIERARCHY_TEMPLATES
        assert len(DATE_HIERARCHY_TEMPLATES[grain]) >= 3


# ---------------------------------------------------------------------------
# D.2 — Batch create endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_date_creates_three_hierarchies(modeler_client):
    """3 unassigned date columns → 3 hierarchies, 3 aliases created."""
    from shared.db.models import CalendarTable, Model, ModelTable

    cal_table_id = uuid.uuid4()
    cal_mt = _make_cal_mt(cal_table_id)
    cal_info = _make_cal_info(cal_table_id)
    date_key_col = _make_date_key_col(cal_mt.id)

    col1 = _make_col("order_date")
    col2 = _make_col("ship_date")
    col3 = _make_col("created_at", "timestamp")

    mock_db = make_mock_db()

    def _get_dispatch(cls, pk):
        if cls is Model:
            return _make_model()
        if cls is ModelTable:
            return cal_mt
        if cls is CalendarTable:
            return cal_info
        return None

    mock_db.get = AsyncMock(side_effect=_get_dispatch)

    # Queries: date key col lookup, then per column:
    #   hierarchy-name check, alias query, reusable-UDA check,
    #   then per level (3 for y_m_d): existing-dimension check
    date_key_result = MagicMock()
    date_key_result.scalar_one_or_none.return_value = date_key_col
    hier_name_check = MagicMock()
    hier_name_check.scalar_one_or_none.return_value = None
    alias_query = MagicMock()
    alias_query.scalars.return_value.all.return_value = []
    reusable_query = MagicMock()
    reusable_query.all.return_value = []
    dim_check = MagicMock()
    dim_check.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[
        date_key_result,
        hier_name_check, alias_query, reusable_query, dim_check, dim_check, dim_check,
        hier_name_check, alias_query, reusable_query, dim_check, dim_check, dim_check,
        hier_name_check, alias_query, reusable_query, dim_check, dim_check, dim_check,
    ])

    unassigned_attrs = [col1, col2, col3]

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies._get_unassigned_date_cols",
            AsyncMock(return_value=unassigned_attrs),
        ),
    ):
        resp = await modeler_client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies/batch-date",
            json={
                "grain": "y_m_d",
                "calendar_table_id": str(cal_mt.id),
                "measure_ids": [],
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created_hierarchies"] == 3
    assert body["created_aliases"] == 3
    assert body["skipped"] == []


@pytest.mark.asyncio
async def test_batch_date_skips_already_assigned(modeler_client):
    """When all date columns are already assigned, response has 0 created."""
    from shared.db.models import CalendarTable, Model, ModelTable

    cal_table_id = uuid.uuid4()
    cal_mt = _make_cal_mt(cal_table_id)
    cal_info = _make_cal_info(cal_table_id)
    date_key_col = _make_date_key_col(cal_mt.id)

    mock_db = make_mock_db()

    def _get_dispatch(cls, pk):
        if cls is Model:
            return _make_model()
        if cls is ModelTable:
            return cal_mt
        if cls is CalendarTable:
            return cal_info
        return None

    mock_db.get = AsyncMock(side_effect=_get_dispatch)

    date_key_result = MagicMock()
    date_key_result.scalar_one_or_none.return_value = date_key_col
    mock_db.execute = AsyncMock(return_value=date_key_result)

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies._get_unassigned_date_cols",
            AsyncMock(return_value=[]),  # nothing unassigned
        ),
    ):
        resp = await modeler_client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies/batch-date",
            json={
                "grain": "y_m_d",
                "calendar_table_id": str(cal_mt.id),
                "measure_ids": [],
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created_hierarchies"] == 0
    assert body["created_aliases"] == 0
    assert body["skipped"] == []


@pytest.mark.asyncio
async def test_batch_date_skips_calendar_table_own_columns(modeler_client):
    """Columns that belong to the calendar table itself are skipped."""
    from shared.db.models import CalendarTable, Model, ModelTable

    cal_table_id = uuid.uuid4()
    cal_mt = _make_cal_mt(cal_table_id)
    cal_info = _make_cal_info(cal_table_id)
    date_key_col = _make_date_key_col(cal_mt.id)

    from src.api.hierarchies import _UnassignedDateAttr
    cal_col_attr = _UnassignedDateAttr(
        id=uuid.uuid4(),
        column_name="date_key",
        display_name="Date Key",
        data_type="date",
        table_id=cal_mt.id,
        table_alias="dim_date",
        is_uda=False,
        physical_column_id=None,
    )

    mock_db = make_mock_db()

    def _get_dispatch(cls, pk):
        if cls is Model:
            return _make_model()
        if cls is ModelTable:
            return cal_mt
        if cls is CalendarTable:
            return cal_info
        return None

    mock_db.get = AsyncMock(side_effect=_get_dispatch)

    date_key_result = MagicMock()
    date_key_result.scalar_one_or_none.return_value = date_key_col
    mock_db.execute = AsyncMock(return_value=date_key_result)

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies._get_unassigned_date_cols",
            AsyncMock(return_value=[cal_col_attr]),
        ),
    ):
        resp = await modeler_client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies/batch-date",
            json={
                "grain": "y_m_d",
                "calendar_table_id": str(cal_mt.id),
                "measure_ids": [],
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created_hierarchies"] == 0
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["reason"] == "column belongs to calendar table"


@pytest.mark.asyncio
async def test_batch_date_invalid_grain_returns_422(modeler_client):
    """An unrecognised grain key returns 422."""
    from shared.db.models import CalendarTable, Model, ModelTable

    cal_table_id = uuid.uuid4()
    cal_mt = _make_cal_mt(cal_table_id)

    mock_db = make_mock_db()

    def _get_dispatch(cls, pk):
        if cls is Model:
            return _make_model()
        if cls is ModelTable:
            return cal_mt
        return None

    mock_db.get = AsyncMock(side_effect=_get_dispatch)

    with patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)):
        resp = await modeler_client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies/batch-date",
            json={
                "grain": "unknown_grain",
                "calendar_table_id": str(cal_mt.id),
                "measure_ids": [],
            },
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unassigned_dates_endpoint_returns_list(modeler_client):
    """GET /columns/unassigned-dates returns list of unassigned date columns."""
    col1 = _make_col("order_date")
    col2 = _make_col("created_at", "timestamp")

    mock_db = make_mock_db()

    from shared.db.models import Model
    mock_db.get = AsyncMock(return_value=_make_model())

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies._get_unassigned_date_cols",
            AsyncMock(return_value=[col1, col2]),
        ),
    ):
        resp = await modeler_client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/columns/unassigned-dates"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    names = {item["column_name"] for item in body}
    assert names == {"order_date", "created_at"}


