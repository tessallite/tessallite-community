"""Typed pivot-view config contract (L18 — Bug-8161 / Bug-8182 / Bug-7442).

A pivot-view config submission that fails VALIDATION must return HTTP 422 whose
body carries a machine ``error_code`` from ``PivotConfigErrorCode`` plus a human
``message`` — so the client maps the failure off a stable token, never off prose.
Before this lane the same submissions returned 400 with a bare string ``detail``
and no ``error_code`` at all, so every assertion here fails against the pre-fix
code (captured in the closeout).

Test escape: ``pivot_views.py`` had NO test module — the ad-hoc 400 validation
was never exercised, so there was nothing to notice that the client had only
prose to recover from. Guard: this module (typed status + error_code per case)
plus the producer/consumer set assertion. Tier: T2 (contract; scope: isolated).
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import Model, Measure, SavedPivotView
from shared.schemas.pydantic_models import (
    ALL_PIVOT_CONFIG_ERROR_CODES,
    PivotConfigErrorCode,
    validate_pivot_config,
    validate_pivot_config_structure,
)
from .conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    TEST_USER_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)
import src.api.pivot_views as pivot_views

pytestmark = pytest.mark.unit

BASE = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/pivot-views"


def _db_with_model(*, measure: object | None = None, dim_ids: list[uuid.UUID] | None = None) -> AsyncMock:
    """Mock session: ``get(Model)`` → the test model, ``get(Measure)`` → *measure*,
    and the dimension-existence ``execute`` returns *dim_ids* as the found set."""
    db = make_mock_db()
    model = make_model()

    async def _get(cls, ident):
        if cls is Model:
            return model
        if cls is Measure:
            return measure
        return None

    db.get = AsyncMock(side_effect=_get)

    found = types.SimpleNamespace()
    result = types.SimpleNamespace()
    result.scalars = lambda: types.SimpleNamespace(all=lambda: list(dim_ids or []))
    db.execute = AsyncMock(return_value=result)

    async def _refresh(obj):
        # A real DB roundtrip assigns the server-default id and timestamps;
        # emulate that so ``_to_response`` can serialise the freshly-created row.
        from .conftest import NOW

        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = NOW
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = NOW
        return None

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


async def _post(client, db, body: dict):
    from unittest.mock import patch

    with patch.object(pivot_views, "get_tenant_db", async_gen_from(db)):
        return await client.post(BASE, json=body)


# ---------------------------------------------------------------------------
# Producer side: the stable vocabulary
# ---------------------------------------------------------------------------

def test_error_code_vocabulary_is_stable() -> None:
    """The finite domain the frontend ERROR_CODE_MAP mirrors. The vitest half
    reads these same tokens from the schema source and asserts an exact match."""
    assert ALL_PIVOT_CONFIG_ERROR_CODES == (
        "INVALID_STRUCTURE",
        "INVALID_MEASURE_ID",
        "UNKNOWN_MEASURE",
        "INVALID_DIMENSION_ID",
        "UNKNOWN_DIMENSION",
    )


def test_validate_pivot_config_empty_measure_is_allowed() -> None:
    """Bug-7442/Bug-6412: an empty measure_id is the 'no primary measure'
    sentinel and is NOT a validation failure."""
    assert validate_pivot_config(None, "", [], []) is None


# ---------------------------------------------------------------------------
# Consumer-facing contract: the route returns typed 422s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_measure_id_returns_typed_422(client) -> None:
    db = _db_with_model()
    resp = await _post(client, db, {
        "name": "v", "measure_id": "not-a-uuid",
        "row_dim_ids": [], "col_dim_ids": [], "config": None,
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == PivotConfigErrorCode.INVALID_MEASURE_ID.value


@pytest.mark.asyncio
async def test_invalid_config_structure_returns_typed_422(client) -> None:
    db = _db_with_model()
    resp = await _post(client, db, {
        "name": "v", "measure_id": "",
        "row_dim_ids": [], "col_dim_ids": [],
        # measureSelections must be a list of {measureId, agg}; a bare string
        # is the exact shape that used to crash the loader (Bug-8161).
        "config": {"measureSelections": "oops"},
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == PivotConfigErrorCode.INVALID_STRUCTURE.value


@pytest.mark.asyncio
async def test_invalid_dimension_id_returns_typed_422(client) -> None:
    db = _db_with_model()
    resp = await _post(client, db, {
        "name": "v", "measure_id": "",
        "row_dim_ids": ["not-a-uuid"], "col_dim_ids": [], "config": None,
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == PivotConfigErrorCode.INVALID_DIMENSION_ID.value


@pytest.mark.asyncio
async def test_unknown_measure_returns_typed_422(client) -> None:
    # Valid UUID, but db.get(Measure) → None: the measure is not in this model.
    db = _db_with_model(measure=None)
    resp = await _post(client, db, {
        "name": "v", "measure_id": str(uuid.uuid4()),
        "row_dim_ids": [], "col_dim_ids": [], "config": None,
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == PivotConfigErrorCode.UNKNOWN_MEASURE.value


@pytest.mark.asyncio
async def test_unknown_dimension_returns_typed_422(client) -> None:
    # Valid dim UUID, but the existence query returns an empty found set.
    dim = str(uuid.uuid4())
    db = _db_with_model(dim_ids=[])
    resp = await _post(client, db, {
        "name": "v", "measure_id": "",
        "row_dim_ids": [dim], "col_dim_ids": [], "config": None,
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == PivotConfigErrorCode.UNKNOWN_DIMENSION.value


@pytest.mark.asyncio
async def test_valid_config_is_accepted(client) -> None:
    # A well-formed measure + dimension that both exist → 201 created.
    measure_id = uuid.uuid4()
    dim_id = uuid.uuid4()
    measure = types.SimpleNamespace(id=measure_id, model_id=TEST_MODEL_ID)
    db = _db_with_model(measure=measure, dim_ids=[dim_id])
    resp = await _post(client, db, {
        "name": "v", "measure_id": str(measure_id),
        "row_dim_ids": [str(dim_id)], "col_dim_ids": [],
        "config": {"measureSelections": [{"measureId": str(measure_id), "agg": "SUM"}]},
    })
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Review B1 — the loader-crashing config fields are now typed and rejected.
# Each payload below is one SlicerBar/PivotGrid dereference away from a crash.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_config", [
    {"slicers": [None]},                       # SlicerBar: s.dimensionId on null
    {"slicers": [{"op": "eq", "values": []}]},  # missing dimensionId
    {"slicers": [{"dimensionId": "d", "op": "bogus", "values": []}]},  # unknown op
    {"conditionalFormat": None},               # PivotGrid: .kind on null
    {"conditionalFormat": "oops"},             # PivotGrid: .kind on a string
    {"conditionalFormat": {"kind": "color-scale", "low": "#fff"}},  # missing high
    {"showSubtotals": "yes"},                  # display toggle, wrong type
    {"emptyCellMode": "purple"},               # not a known mode
    {"measureSelections": [None]},             # buildColumnMeasures: sel.measureId on null
], ids=[
    "slicer_null", "slicer_no_dim", "slicer_bad_op",
    "cf_null", "cf_string", "cf_missing_field",
    "bool_wrong_type", "empty_cell_mode_bad", "measure_sel_null",
])
async def test_malformed_config_field_returns_invalid_structure(client, bad_config) -> None:
    db = _db_with_model()
    resp = await _post(client, db, {
        "name": "v", "measure_id": "",
        "row_dim_ids": [], "col_dim_ids": [], "config": bad_config,
    })
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == PivotConfigErrorCode.INVALID_STRUCTURE.value


@pytest.mark.asyncio
@pytest.mark.parametrize("good_config", [
    {"slicers": [{"dimensionId": "d1", "op": "eq", "values": ["x"]}]},
    {"conditionalFormat": {"kind": "none"}},
    {"conditionalFormat": {"kind": "color-scale", "low": "#fff", "high": "#000"}},
    {"showSubtotals": True, "showGrandTotals": False, "forceLive": True},
    {"emptyCellMode": "zero"},
    {"someFutureKey": {"a": 1}},               # unknown extra key: forward-compat
    {"extraMeasureIds": ["a", "b"], "measureAggOverrides": {"a": "SUM"}},  # legacy
], ids=[
    "valid_slicer", "cf_none", "cf_color_scale", "toggles",
    "empty_cell_mode", "unknown_extra_key", "legacy_fields",
])
async def test_wellformed_config_is_accepted(client, good_config) -> None:
    # A legitimate current-or-historical config must NOT be rejected (backward /
    # forward compat). measure_id empty + no dims so only structure is exercised.
    db = _db_with_model()
    resp = await _post(client, db, {
        "name": "v", "measure_id": "",
        "row_dim_ids": [], "col_dim_ids": [], "config": good_config,
    })
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Review B2 — publishing a malformed HISTORICAL config revalidates structure.
# ---------------------------------------------------------------------------

def _make_view(*, config_json, is_shared=False, measure_id="", created_by=TEST_USER_ID):
    return types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, name="v",
        measure_id=measure_id, row_dim_ids="[]", col_dim_ids="[]",
        config_json=config_json, created_by=created_by, is_shared=is_shared,
        created_at=NOW, updated_at=NOW,
    )


def _db_with_view(view, *, measure=None) -> AsyncMock:
    db = make_mock_db()
    model = make_model()

    async def _get(cls, ident):
        if cls is Model:
            return model
        if cls is SavedPivotView:
            return view
        if cls is Measure:
            return measure
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()  # view already carries id/timestamps
    return db


async def _patch(client, db, view_id, body):
    with patch.object(pivot_views, "get_tenant_db", async_gen_from(db)):
        return await client.patch(f"{BASE}/{view_id}", json=body)


@pytest.mark.asyncio
async def test_publishing_malformed_historical_config_is_rejected_and_not_shared(client) -> None:
    # A personal view whose config predates the typed contract carries a
    # loader-crashing slicers:[null]. Publishing it tenant-wide must 422 and
    # leave is_shared FALSE, so no other user can load the crashing view.
    view = _make_view(config_json=json.dumps({"slicers": [None]}), is_shared=False)
    db = _db_with_view(view)
    resp = await _patch(client, db, view.id, {"is_shared": True})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == PivotConfigErrorCode.INVALID_STRUCTURE.value
    assert view.is_shared is False


@pytest.mark.asyncio
async def test_publishing_wellformed_view_still_succeeds(client) -> None:
    # Regression: a valid personal view still publishes (B2 must not block it).
    view = _make_view(
        config_json=json.dumps({"conditionalFormat": {"kind": "none"}}),
        is_shared=False,
    )
    db = _db_with_view(view)
    resp = await _patch(client, db, view.id, {"is_shared": True})
    assert resp.status_code == 200, resp.text
    assert view.is_shared is True


# ---------------------------------------------------------------------------
# Review B5 — the full measure_id null/empty/UUID contract.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_null_measure_id_is_rejected(client) -> None:
    # POST measure_id null is invalid (PivotViewCreate.measure_id is non-nullable);
    # FastAPI request validation returns 422.
    db = _db_with_model()
    resp = await _post(client, db, {
        "name": "v", "measure_id": None,
        "row_dim_ids": [], "col_dim_ids": [], "config": None,
    })
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_null_measure_id_leaves_pointer_unchanged(client) -> None:
    # PATCH measure_id null = "leave the stored pointer unchanged". The stored
    # pointer resolves to a real model measure, so the (effective) existence check
    # passes and the value is not rewritten.
    measure_id = uuid.uuid4()
    measure = types.SimpleNamespace(id=measure_id, model_id=TEST_MODEL_ID)
    view = _make_view(config_json=None, measure_id=str(measure_id))
    db = _db_with_view(view, measure=measure)
    resp = await _patch(client, db, view.id, {"measure_id": None})
    assert resp.status_code == 200, resp.text
    assert view.measure_id == str(measure_id)  # unchanged


@pytest.mark.asyncio
async def test_patch_empty_measure_id_clears_pointer(client) -> None:
    # PATCH measure_id "" = clear to the synthetic / no-primary sentinel.
    view = _make_view(config_json=None, measure_id=str(uuid.uuid4()))
    db = _db_with_view(view)
    resp = await _patch(client, db, view.id, {"measure_id": ""})
    assert resp.status_code == 200, resp.text
    assert view.measure_id == ""


def test_validate_pivot_config_structure_ignores_measure_id() -> None:
    # The structure-only validator used by the publish path must not inspect
    # measure_id/dims — a legacy pointer is a referential, not structural, matter.
    assert validate_pivot_config_structure({"slicers": [None]}) is not None
    assert validate_pivot_config_structure({"conditionalFormat": {"kind": "none"}}) is None
    assert validate_pivot_config_structure(None) is None
