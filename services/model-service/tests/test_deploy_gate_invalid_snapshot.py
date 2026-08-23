"""F-013-14 / F-101-11 / F-016-12 (Bug-9052) — deploy refuses invalid snapshots.

``_validate_snapshot_for_deploy`` previously proved only that a snapshot had
SHAPE (some measures/dimensions/columns). It did not prove the content was
SERVABLE, so a model with an ``is_invalid`` measure/dimension (the binder skips
the matcher and falls back to source for these) or a UDA that never validated
(a generated date-hierarchy key invalidated by schema drift) published cleanly
and then failed / misrouted at query time — Deploy looked like a health
certificate it was not. The gate now refuses these with a 409.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from src.api.versions import _validate_snapshot_for_deploy


def _base_snapshot(**extra):
    snap = {
        "schema_version": 5,
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        "dimensions": [{"id": str(uuid.uuid4()), "name": "region"}],
        "columns": [{"id": str(uuid.uuid4()), "column_name": "amount"}],
    }
    snap.update(extra)
    return snap


def _deploy(snapshot):
    _validate_snapshot_for_deploy(snapshot, uuid.uuid4(), 5)


def test_valid_snapshot_passes():
    # No is_invalid / unvalidated content -> no raise.
    _deploy(_base_snapshot())


def test_invalid_measure_is_refused():
    snap = _base_snapshot(
        measures=[{"id": str(uuid.uuid4()), "name": "broken", "is_invalid": True}]
    )
    with pytest.raises(HTTPException) as ei:
        _deploy(snap)
    assert ei.value.status_code == 409
    assert "broken" in ei.value.detail


def test_invalid_dimension_is_refused():
    snap = _base_snapshot(
        dimensions=[{"id": str(uuid.uuid4()), "name": "bad_dim", "is_invalid": True}]
    )
    with pytest.raises(HTTPException) as ei:
        _deploy(snap)
    assert ei.value.status_code == 409
    assert "bad_dim" in ei.value.detail


def test_unvalidated_uda_is_refused():
    snap = _base_snapshot(
        user_defined_attributes=[
            {"id": str(uuid.uuid4()), "name": "fy_year", "validated": False}
        ]
    )
    with pytest.raises(HTTPException) as ei:
        _deploy(snap)
    assert ei.value.status_code == 409
    assert "fy_year" in ei.value.detail


def test_uda_with_validation_error_is_refused():
    snap = _base_snapshot(
        user_defined_attributes=[
            {
                "id": str(uuid.uuid4()),
                "name": "fy_qtr",
                "validated": True,
                "validation_error": "column dropped",
            }
        ]
    )
    with pytest.raises(HTTPException) as ei:
        _deploy(snap)
    assert ei.value.status_code == 409
    assert "fy_qtr" in ei.value.detail


def test_validated_uda_passes():
    snap = _base_snapshot(
        user_defined_attributes=[
            {"id": str(uuid.uuid4()), "name": "fy_year", "validated": True}
        ]
    )
    _deploy(snap)  # no raise


def test_bug_8614_multi_table_snapshot_without_fact_is_rejected_at_deploy():
    snap = _base_snapshot(
        tables=[
            {"id": "dim-a", "physical_name": "customers", "table_type": "dim_detail"},
            {"id": "dim-b", "physical_name": "regions", "table_type": "dim_detail"},
        ]
    )
    with pytest.raises(HTTPException) as ei:
        _deploy(snap)
    assert ei.value.status_code == 409
    assert "exactly one fact table" in ei.value.detail


def test_bug_8614_single_table_snapshot_is_implicitly_fact():
    snap = _base_snapshot(
        tables=[{"id": "one", "physical_name": "events", "table_type": "dim_detail"}]
    )
    _deploy(snap)
