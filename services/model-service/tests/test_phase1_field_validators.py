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


# ---------------------------------------------------------------------------
# Bug-7181: variant_n validation on MeasureCreate
# ---------------------------------------------------------------------------


def _variant_measure(**overrides):
    """Base fields for a variant measure creation."""
    kw = dict(
        name="revenue_trailing",
        source_table_id=uuid.uuid4(),
        source_column_name="amount",
        variant_kind="trailing_n",
        variant_of_measure_id=uuid.uuid4(),
        variant_n=12,
    )
    kw.update(overrides)
    return kw


def test_variant_n_required_for_trailing_n():
    """Bug-7181: trailing_n must require variant_n."""
    with pytest.raises(ValidationError, match="variant_n is required"):
        MeasureCreate(**_variant_measure(variant_n=None))


def test_variant_n_required_for_moving_avg_n():
    """Bug-7181: moving_avg_n must require variant_n."""
    with pytest.raises(ValidationError, match="variant_n is required"):
        MeasureCreate(**_variant_measure(
            variant_kind="moving_avg_n",
            variant_n=None,
        ))


def test_variant_n_rejects_zero():
    """Bug-7181: variant_n=0 is a degenerate single-row window."""
    with pytest.raises(ValidationError, match="variant_n must be >= 1"):
        MeasureCreate(**_variant_measure(variant_n=0))


def test_variant_n_rejects_negative():
    """Bug-7181: negative variant_n is nonsensical."""
    with pytest.raises(ValidationError, match="variant_n must be >= 1"):
        MeasureCreate(**_variant_measure(variant_n=-5))


def test_variant_n_rejects_absurdly_large():
    """Bug-7181: absurdly large variant_n is a DoS risk on some engines."""
    with pytest.raises(ValidationError, match="variant_n must be <= 1000"):
        MeasureCreate(**_variant_measure(variant_n=10_000))


def test_variant_n_accepts_valid_value():
    """Regression: a valid variant_n=12 must pass."""
    m = MeasureCreate(**_variant_measure(variant_n=12))
    assert m.variant_n == 12


def test_variant_n_accepts_boundary_1():
    """Boundary: variant_n=1 is a current-row-only window (valid)."""
    m = MeasureCreate(**_variant_measure(variant_n=1))
    assert m.variant_n == 1


def test_variant_n_accepts_boundary_1000():
    """Boundary: variant_n=1000 is the maximum allowed value."""
    m = MeasureCreate(**_variant_measure(variant_n=1000))
    assert m.variant_n == 1000


def test_non_parametric_variant_no_variant_n_required():
    """Non-parametric variants (e.g. ytd) must NOT require variant_n."""
    m = MeasureCreate(**_variant_measure(
        variant_kind="ytd",
        variant_n=None,
    ))
    assert m.variant_n is None


# ---------------------------------------------------------------------------
# Bug-7181 (codex F3): variant_n range on MeasureUpdate
# ---------------------------------------------------------------------------


def test_measure_update_rejects_zero_variant_n():
    """MeasureUpdate must also reject variant_n=0."""
    with pytest.raises(ValidationError, match="variant_n must be >= 1"):
        MeasureUpdate(variant_n=0)


def test_measure_update_rejects_negative_variant_n():
    """MeasureUpdate must also reject negative variant_n."""
    with pytest.raises(ValidationError, match="variant_n must be >= 1"):
        MeasureUpdate(variant_n=-3)


def test_measure_update_rejects_oversized_variant_n():
    """MeasureUpdate must also reject variant_n > 1000."""
    with pytest.raises(ValidationError, match="variant_n must be <= 1000"):
        MeasureUpdate(variant_n=5000)


def test_measure_update_accepts_valid_variant_n():
    """Regression: a valid variant_n on update must pass."""
    u = MeasureUpdate(variant_n=24)
    assert u.variant_n == 24


def test_measure_update_accepts_null_variant_n():
    """Null variant_n on update means 'not supplied' (no-op)."""
    u = MeasureUpdate(variant_n=None)
    assert u.variant_n is None
