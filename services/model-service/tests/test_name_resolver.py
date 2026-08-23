"""Unit tests for the attribute name uniqueness resolver and rename-preview endpoint.

Covers:
  * resolve_unique_name() — all priority levels and case-insensitive collision
  * GET /{table_id}/rename-preview — happy path and validation
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api._name_resolver import resolve_unique_name

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# resolve_unique_name — pure-function tests (no fixtures needed)
# ---------------------------------------------------------------------------

def test_bare_name_when_no_conflict():
    taken: set[str] = set()
    assert resolve_unique_name("amount", "sales", "fact_sales", taken) == "amount"


def test_alias_prefix_when_bare_taken():
    taken = {"amount"}
    assert resolve_unique_name("amount", "sales", "fact_sales", taken) == "sales_amount"


def test_physical_prefix_when_alias_taken():
    taken = {"amount", "sales_amount"}
    assert resolve_unique_name("amount", "sales", "fact_sales", taken) == "fact_sales_amount"


def test_sequential_fallback_skips_n2_when_needed():
    taken = {"amount", "sales_amount", "fact_sales_amount", "sales_2_amount"}
    result = resolve_unique_name("amount", "sales", "fact_sales", taken)
    assert result == "sales_3_amount"


def test_sequential_continues_incrementing():
    taken = {"amount", "sales_amount", "fact_sales_amount",
             "sales_2_amount", "sales_3_amount", "sales_4_amount"}
    result = resolve_unique_name("amount", "sales", "fact_sales", taken)
    assert result == "sales_5_amount"


def test_case_insensitive_collision():
    # taken uses lowercase; bare name 'Amount' still sees the collision.
    taken = {"amount"}
    assert resolve_unique_name("Amount", "sales", "fact_sales", taken) == "sales_Amount"


def test_alias_prefix_case_insensitive_collision():
    taken = {"amount", "sales_amount"}
    # 'SALES_amount' lowercased is 'sales_amount', must skip to physical.
    assert resolve_unique_name("amount", "SALES", "fact_sales", taken) == "fact_sales_amount"


def test_no_mutation_of_taken_set():
    """resolve_unique_name must not modify the caller's taken set."""
    taken: set[str] = set()
    resolve_unique_name("amount", "sales", "fact_sales", taken)
    assert len(taken) == 0


def test_schema_prefixed_physical_name_stripped_by_caller():
    """Backend strips schema prefix before passing physical_name; verify correct fallback."""
    # physical_name = "orders" (schema already stripped by caller)
    taken = {"revenue", "sales_revenue"}
    assert resolve_unique_name("revenue", "sales", "orders", taken) == "orders_revenue"


def test_resolver_handles_single_word_alias():
    taken = set()
    assert resolve_unique_name("id", "payments", "fact_payments", taken) == "id"


# ---------------------------------------------------------------------------
# rename_preview endpoint — integration tests with mocked DB
# ---------------------------------------------------------------------------

def _make_table(model_id, source_id, physical_name, alias):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.model_id = model_id
    t.source_id = source_id
    t.physical_name = physical_name
    t.alias = alias
    return t


def _col_row(col_id, col_name):
    r = MagicMock()
    r.id = col_id
    r.column_name = col_name
    return r


def _dim(col_id, name, model_id):
    d = MagicMock()
    d.id = uuid.uuid4()
    d.model_id = model_id
    d.source_column_id = col_id
    d.name = name
    return d


def _meas(col_id, name, model_id):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.model_id = model_id
    m.source_column_id = col_id
    m.name = name
    return m


def _url(project_id, model_id, source_id, table_id):
    return (
        f"/api/v1/projects/{project_id}/models/{model_id}"
        f"/sources/{source_id}/tables/{table_id}/rename-preview"
    )


def _build_db(table, col_rows, dims, meas, project_id=None):
    """Minimal async DB mock for the rename-preview handler.

    The endpoint issues 3 execute calls in order:
      1. ModelColumn query          → .all() returns col_rows
      2. Dimension query            → .scalars().all() returns dims
      3. Measure query              → .scalars().all() returns meas
    """
    db = AsyncMock()
    db.info = {"tenant_id": "test-tenant"}

    async def _get(entity, pk):
        # Bug-8862: rename-preview now proves project -> model before it reads
        # the table, so the double must answer the Model lookup too.
        if entity.__name__ == "Model":
            return SimpleNamespace(id=pk, project_id=project_id)
        if pk == table.id:
            return table
        return None

    db.get.side_effect = _get

    col_result = MagicMock()
    col_result.all.return_value = col_rows

    dim_result = MagicMock()
    dim_result.scalars.return_value.all.return_value = dims

    meas_result = MagicMock()
    meas_result.scalars.return_value.all.return_value = meas

    db.execute = AsyncMock(side_effect=[col_result, dim_result, meas_result])
    return db


@pytest.mark.asyncio
async def test_rename_preview_returns_empty_when_no_conflict(client):
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()
    table = _make_table(model_id, source_id, "fact_sales", "sales")
    col_id = uuid.uuid4()
    dim = _dim(col_id, "revenue", model_id)  # name already == col_name → no change

    db = _build_db(table, [_col_row(col_id, "revenue")], [dim], [], project_id=project_id)

    with patch("src.api.tables.get_tenant_db") as mock_gen, \
         patch("src.api.tables._validate_alias_format"):
        async def _gen(tid):
            yield db
        mock_gen.side_effect = _gen

        resp = await client.get(
            _url(project_id, model_id, source_id, table.id),
            params={"new_alias": "sales_new"},
        )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_rename_preview_returns_changed_dimension(client):
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()
    table = _make_table(model_id, source_id, "fact_sales", "sales")
    col_id = uuid.uuid4()
    # Attr currently stored as "sales_branch_id" (old alias prefix).
    # Under new_alias="newco" with no taken names, resolver returns "branch_id".
    dim = _dim(col_id, "sales_branch_id", model_id)

    db = _build_db(table, [_col_row(col_id, "branch_id")], [dim], [], project_id=project_id)

    with patch("src.api.tables.get_tenant_db") as mock_gen, \
         patch("src.api.tables._validate_alias_format"):
        async def _gen(tid):
            yield db
        mock_gen.side_effect = _gen

        resp = await client.get(
            _url(project_id, model_id, source_id, table.id),
            params={"new_alias": "newco"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    row = data[0]
    assert row["type"] == "dimension"
    assert row["source_column_name"] == "branch_id"
    assert row["current_name"] == "sales_branch_id"
    assert row["suggested_name"] == "branch_id"


@pytest.mark.asyncio
async def test_rename_preview_404_when_table_not_found(client):
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()

    db = AsyncMock()
    db.info = {"tenant_id": "test-tenant"}

    async def _get(entity, pk):
        # The model resolves correctly; only the table is missing, so the 404
        # asserted below is the TABLE 404 and not Bug-8862's model 404.
        if entity.__name__ == "Model":
            return SimpleNamespace(id=pk, project_id=project_id)
        return None

    db.get.side_effect = _get

    with patch("src.api.tables.get_tenant_db") as mock_gen, \
         patch("src.api.tables._validate_alias_format"):
        async def _gen(tid):
            yield db
        mock_gen.side_effect = _gen

        resp = await client.get(
            _url(project_id, model_id, source_id, table_id),
            params={"new_alias": "newco"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "ModelTable not found"


@pytest.mark.asyncio
async def test_rename_preview_invalid_alias_returns_400(client):
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()

    # Do NOT patch _validate_alias_format — let real validator run.
    with patch("src.api.tables.get_tenant_db"):
        resp = await client.get(
            _url(project_id, model_id, source_id, table_id),
            params={"new_alias": "bad alias!"},
        )

    assert resp.status_code == 400
