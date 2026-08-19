"""Contract tests: KPI wizard-offered values accepted by API schema.

Bug-7236: every aggregation mode offered by the wizard must be accepted by
the API schema (KPICreate/KPIUpdate).
Bug-7237: every target type offered by the wizard (including "none") must
be accepted by the API schema.

These tests verify producer/consumer alignment between the frontend wizard
picker values and the backend Pydantic validators, preventing regressions
where the enum is narrowed without updating the wizard (or vice versa).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.schemas.domains.governance_advanced import KPICreate, KPIUpdate

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Canonical wizard-offered values (match frontend types_domains/kpis.ts and
# wizard component MenuItems exactly)
# ---------------------------------------------------------------------------

WIZARD_CALC_AGG_MODES = [
    "automatic",
    "aggregate_first",
    "row_first",
    "aggregate_of_aggregate",
    "pre_aggregated",
]

WIZARD_TARGET_TYPES = [
    "none",
    "static",
    "measure",
    "prior_period",
    "expression",
]


# ---------------------------------------------------------------------------
# Bug-7236: aggregation mode acceptance
# ---------------------------------------------------------------------------

class TestCalcAggModeContract:
    """Every calc_agg_mode the wizard offers must be accepted by KPICreate
    and KPIUpdate without raising a ValidationError."""

    @pytest.mark.parametrize("mode", WIZARD_CALC_AGG_MODES)
    def test_create_accepts_wizard_agg_mode(self, mode: str):
        payload = KPICreate(name="contract_test", calc_agg_mode=mode)
        assert payload.calc_agg_mode == mode

    @pytest.mark.parametrize("mode", WIZARD_CALC_AGG_MODES)
    def test_update_accepts_wizard_agg_mode(self, mode: str):
        payload = KPIUpdate(calc_agg_mode=mode)
        assert payload.calc_agg_mode == mode

    def test_invalid_agg_mode_rejected(self):
        with pytest.raises(ValidationError, match="calc_agg_mode"):
            KPICreate(name="contract_test", calc_agg_mode="nonexistent_mode")

    def test_invalid_agg_mode_rejected_on_update(self):
        with pytest.raises(ValidationError, match="calc_agg_mode"):
            KPIUpdate(calc_agg_mode="nonexistent_mode")


# ---------------------------------------------------------------------------
# Bug-7237: target type acceptance
# ---------------------------------------------------------------------------

class TestTargetTypeContract:
    """Every target_type the wizard offers must be accepted by KPICreate
    and KPIUpdate.  The string "none" must be normalised to None (SQL NULL)."""

    @pytest.mark.parametrize("tt", WIZARD_TARGET_TYPES)
    def test_create_accepts_wizard_target_type(self, tt: str):
        payload = KPICreate(name="contract_test", target_type=tt)
        # "none" should be normalised to None, others kept as-is.
        if tt == "none":
            assert payload.target_type is None
        else:
            assert payload.target_type == tt

    @pytest.mark.parametrize("tt", WIZARD_TARGET_TYPES)
    def test_update_accepts_wizard_target_type(self, tt: str):
        payload = KPIUpdate(target_type=tt)
        if tt == "none":
            assert payload.target_type is None
        else:
            assert payload.target_type == tt

    def test_null_target_type_accepted(self):
        """Omitting target_type (None) is valid — equivalent to no target."""
        payload = KPICreate(name="contract_test", target_type=None)
        assert payload.target_type is None

    def test_invalid_target_type_rejected(self):
        with pytest.raises(ValidationError, match="target_type"):
            KPICreate(name="contract_test", target_type="nonexistent_type")
