"""Phase 1 — closed-enum field validators on Pydantic DTOs.

The save-time guards on ``measure.format``, ``hierarchy_level.time_unit``,
and ``hierarchy_level.allowed_time_calcs`` enforce the shared catalogs
(`MEASURE_FORMAT_TOKENS`, `HIERARCHY_TIME_UNITS`, `HIERARCHY_TIME_CALCS`)
before an unsupported token ever reaches the DB. The frontend also
validates but the backend must never trust the client.
"""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from shared.schemas.pydantic_models import (
    HierarchyLevelCreate,
    HierarchyLevelUpdate,
    MeasureCreate,
    MeasureUpdate,
)

pytestmark = pytest.mark.unit


def _base_measure(**overrides):
    kw = dict(
        name="revenue",
        source_table_id=uuid.uuid4(),
        source_column_name="amount",
    )
    kw.update(overrides)
    return kw


def test_measure_create_rejects_unknown_format():
    with pytest.raises(ValidationError) as exc:
        MeasureCreate(**_base_measure(format="bogus_format"))
    assert "format" in str(exc.value)


def test_measure_create_accepts_known_format():
    m = MeasureCreate(**_base_measure(format="percent"))
    assert m.format == "percent"


def test_measure_create_accepts_null_format():
    m = MeasureCreate(**_base_measure(format=None))
    assert m.format is None


def test_measure_update_rejects_unknown_format():
    with pytest.raises(ValidationError):
        MeasureUpdate(format="not_a_token")


def test_measure_update_accepts_known_format():
    assert MeasureUpdate(format="currency").format == "currency"


def _level(**overrides):
    kw = dict(
        name="year",
        ordinal=0,
        key_attribute_id=uuid.uuid4(),
        key_attribute_source="physical_column",
    )
    kw.update(overrides)
    return kw


def test_level_create_rejects_unknown_time_unit():
    with pytest.raises(ValidationError) as exc:
        HierarchyLevelCreate(**_level(time_unit="fortnight"))
    assert "time_unit" in str(exc.value)


def test_level_create_accepts_known_time_unit():
    lvl = HierarchyLevelCreate(**_level(time_unit="quarter"))
    assert lvl.time_unit == "quarter"


def test_level_create_rejects_unknown_time_calc_token():
    with pytest.raises(ValidationError) as exc:
        HierarchyLevelCreate(**_level(
            time_unit="year",
            allowed_time_calcs=["period_to_date", "ytd_prior_year"],  # 2nd is a variant, not a family
        ))
    assert "allowed_time_calcs" in str(exc.value)


def test_level_create_accepts_valid_time_calcs():
    lvl = HierarchyLevelCreate(**_level(
        time_unit="year",
        allowed_time_calcs=["period_to_date", "parallel_period"],
    ))
    assert set(lvl.allowed_time_calcs) == {"period_to_date", "parallel_period"}


def test_level_update_rejects_unknown_time_unit():
    with pytest.raises(ValidationError):
        HierarchyLevelUpdate(time_unit="centuries")


def test_level_update_rejects_unknown_time_calc_token():
    with pytest.raises(ValidationError):
        HierarchyLevelUpdate(allowed_time_calcs=["bogus"])


def test_level_update_accepts_null_collections():
    u = HierarchyLevelUpdate(time_unit=None, allowed_time_calcs=None)
    assert u.time_unit is None
    assert u.allowed_time_calcs is None
