"""Bug-7183: save-time gate — reject calc measures referencing semi-additive measures.

A calculated measure that references a semi-additive measure (e.g. a
last_non_empty balance) is semantically invalid: the query-router expands
calc expressions via the raw column wrapped in the default agg, but
semi-additive measures require specialised window aggregation. This test
ensures both the create and update endpoints reject such references with
a clear 400 error, and that the validate-expression endpoint returns
``valid=false`` for the same case.

Mirrors the existing variant-reference gate (F-015-04) tested in
``test_validate_expression_endpoint.py::test_validate_expression_rejects_variant_reference``.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import Measure
from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"


def _patch_scope():
    async def _noop(db, *, project_id, model_id):
        return None
    return patch("src.api.measures.ensure_model_in_project", _noop)


def _measure_row(
    name: str,
    *,
    measure_type: str = "standard",
    variant_kind: str | None = None,
    semi_additive_behavior: str | None = None,
    expression: str | None = None,
    measure_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=measure_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        measure_type=measure_type,
        variant_kind=variant_kind,
        semi_additive_behavior=semi_additive_behavior,
        expression=expression,
    )


def _db_with_measures(measures: list) -> AsyncMock:
    """Build a mock DB whose execute(select(Measure)) returns the given rows."""
    db = make_mock_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(measures)
    db.execute = AsyncMock(return_value=result)
    return db


# -------------------------------------------------------------------
# 1. validate-expression endpoint rejects semi-additive references
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_expression_rejects_semi_additive_reference(client):
    """The validate-expression endpoint must return valid=false when the
    expression references a semi-additive measure."""
    balance = _measure_row("balance", semi_additive_behavior="last_non_empty")
    revenue = _measure_row("revenue")
    db = _db_with_measures([balance, revenue])

    body = {"expression": 'safe_div(measure("balance"), measure("revenue"))'}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.post(f"{PREFIX}/validate-expression", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["valid"] is False
    assert "balance" in payload["error"]
    assert "semi-additive" in payload["error"].lower()


# -------------------------------------------------------------------
# 2. create endpoint rejects calc measure referencing semi-additive
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_calc_measure_rejects_semi_additive_reference(client):
    """POST /measures with measure_type=calculated must reject an expression
    referencing a semi-additive measure with 400."""
    balance = _measure_row("balance", semi_additive_behavior="last_non_empty")
    revenue = _measure_row("revenue")
    db = _db_with_measures([balance, revenue])

    body = {
        "name": "bad_ratio",
        "measure_type": "calculated",
        "expression": 'safe_div(measure("balance"), measure("revenue"))',
        "calc_agg_mode": "expression_as_written",
    }

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.post(PREFIX, json=body)

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "balance" in detail
    assert "semi-additive" in detail.lower()


# -------------------------------------------------------------------
# 3. update endpoint rejects calc measure expression change to
#    semi-additive reference
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_calc_measure_rejects_semi_additive_reference(client):
    """PATCH /measures/{id} changing expression to one referencing a
    semi-additive measure must reject with 400."""
    calc_id = uuid.uuid4()
    balance = _measure_row("balance", semi_additive_behavior="first_non_empty")
    db = _db_with_measures([balance])

    # The existing calculated measure being updated
    existing_calc = types.SimpleNamespace(
        id=calc_id,
        model_id=TEST_MODEL_ID,
        name="ratio",
        display_name="Ratio",
        description=None,
        display_folder=None,
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type="calculated",
        expression='measure("revenue")',
        calc_agg_mode="auto",
        data_type="numeric",
        default_agg="SUM",
        format="decimal",
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_additive=True,
        semi_additive_behavior=None,
        semi_additive_account_column_id=None,
        calendar_model_table_id=None,
        hierarchy_id=None,
        date_dimension_column_id=None,
        resolved_calendar_id=None,
        resolved_date_col_id=None,
        is_invalid=False,
        invalid_reason=None,
        cross_model_source_model_id=None,
        cross_model_source_measure_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    db.get = AsyncMock(return_value=existing_calc)

    body = {
        "expression": 'measure("balance") * 2',
    }

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.patch(f"{PREFIX}/{calc_id}", json=body)

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "balance" in detail
    assert "semi-additive" in detail.lower()


# -------------------------------------------------------------------
# 4. create calc measure succeeds when no semi-additive references
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_calc_measure_accepts_non_semi_additive(client):
    """POST /measures with measure_type=calculated must succeed when the
    expression references only standard (non-semi-additive) measures."""
    revenue = _measure_row("revenue")
    cost = _measure_row("cost")

    db = make_mock_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [revenue, cost]
    db.execute = AsyncMock(return_value=result)

    async def _refresh(obj):
        obj.id = obj.id or uuid.uuid4()
        obj.created_at = obj.created_at or NOW
        obj.updated_at = obj.updated_at or NOW
        obj.is_hidden = getattr(obj, "is_hidden", False)
        obj.is_invalid = getattr(obj, "is_invalid", False)
        obj.invalid_reason = getattr(obj, "invalid_reason", None)
    db.refresh = AsyncMock(side_effect=_refresh)

    body = {
        "name": "margin",
        "measure_type": "calculated",
        "expression": 'safe_div(measure("revenue") - measure("cost"), measure("revenue"))',
        "calc_agg_mode": "expression_as_written",
    }

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ), patch(
        "src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)
    ):
        resp = await client.post(PREFIX, json=body)

    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["name"] == "margin"
    assert payload["measure_type"] == "calculated"


# -------------------------------------------------------------------
# 5. validate-expression accepts non-semi-additive references
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_expression_accepts_non_semi_additive(client):
    """The validate-expression endpoint must return valid=true when the
    expression references only standard measures (no semi-additive)."""
    revenue = _measure_row("revenue")
    cost = _measure_row("cost")
    db = _db_with_measures([revenue, cost])

    body = {"expression": 'measure("revenue") - measure("cost")'}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.post(f"{PREFIX}/validate-expression", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["valid"] is True
    assert set(payload["referenced_measure_names"]) == {"revenue", "cost"}
