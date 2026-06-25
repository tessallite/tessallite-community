"""POST /measures/validate-expression — Phase 4B frontend validation contract.

The endpoint reuses ``_resolve_calculated_expression`` so the same rules
(single-pass, allow-listed functions, cycle detection, reference resolution)
apply to live frontend validation and to save-time enforcement.

Covers:
  * Happy path returns ``valid=true`` with the list of referenced names + ids.
  * Malformed expression returns ``valid=false`` with a diagnostic string, not 400.
  * Unknown references return ``valid=false`` naming the missing measures.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
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
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        measure_type=measure_type,
        variant_kind=variant_kind,
        expression=None,
    )


def _db_with_measures(measures: list) -> AsyncMock:
    """Build a mock DB whose execute(select(Measure)) returns the given rows."""
    db = make_mock_db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(measures)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_validate_expression_happy_path(client):
    gm = _measure_row("gm")
    sales = _measure_row("sales")
    db = _db_with_measures([gm, sales])

    body = {"expression": 'safe_div(measure("gm"), measure("sales"))'}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.post(f"{PREFIX}/validate-expression", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["valid"] is True
    assert payload["error"] is None
    assert set(payload["referenced_measure_names"]) == {"gm", "sales"}
    assert set(payload["referenced_measure_ids"]) == {str(gm.id), str(sales.id)}


@pytest.mark.asyncio
async def test_validate_expression_malformed_returns_valid_false(client):
    db = _db_with_measures([])
    body = {"expression": "this is not a real expression"}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.post(f"{PREFIX}/validate-expression", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["valid"] is False
    assert isinstance(payload["error"], str) and payload["error"]
    assert payload["referenced_measure_names"] == []


@pytest.mark.asyncio
async def test_validate_expression_unknown_reference_returns_valid_false(client):
    gm = _measure_row("gm")  # only "gm" exists; expression references "sales" too
    db = _db_with_measures([gm])

    body = {"expression": 'measure("gm") / measure("sales")'}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.post(f"{PREFIX}/validate-expression", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["valid"] is False
    assert "sales" in payload["error"]


@pytest.mark.asyncio
async def test_validate_expression_rejects_variant_reference(client):
    # F-015-04: a calculated measure that references a time-variant measure
    # silently rewrote to the variant's base column (e.g. measure("rev_ytd")
    # -> SUM(rev)), so safe_div(measure("rev_ytd"), measure("rev")) collapsed
    # to constant 1.0. The reference must be rejected at save/validate time.
    rev = _measure_row("rev")
    rev_ytd = _measure_row("rev_ytd", variant_kind="ytd")
    db = _db_with_measures([rev, rev_ytd])

    body = {"expression": 'safe_div(measure("rev_ytd"), measure("rev"))'}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(db)
    ):
        resp = await client.post(f"{PREFIX}/validate-expression", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["valid"] is False
    assert "rev_ytd" in payload["error"]
    assert "variant" in payload["error"].lower()
