"""Bug-377: create_measure must enforce variant eligibility rules.

Previously _check_variant_eligibility was advisory-only (called by
/available-variants but not by the create endpoint). Now the create
endpoint calls it and returns 422 when the variant is ineligible.
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


def _col_row(col_id, table_id):
    return types.SimpleNamespace(
        id=col_id,
        column_name="amount",
        model_table_id=table_id,
        data_type="numeric",
    )


def _make_db_for_variant(col_id, table_id, *, base_measure=None):
    mock_db = make_mock_db()
    col = _col_row(col_id, table_id)

    async def _exec(stmt):
        text = str(stmt)
        r = MagicMock()
        if "model_columns" in text.lower():
            r.scalar_one_or_none.return_value = col
        else:
            r.scalar_one_or_none.return_value = None
            r.scalars.return_value.all.return_value = []
            r.first.return_value = None
        return r

    mock_db.execute = AsyncMock(side_effect=_exec)

    async def _get(cls, key):
        if key == col_id:
            return types.SimpleNamespace(
                id=col_id,
                column_name="amount",
                model_table_id=table_id,
                is_hidden=False,
            )
        if base_measure and key == base_measure.id:
            return base_measure
        return None

    mock_db.get = AsyncMock(side_effect=_get)

    async def _refresh(obj):
        obj.id = obj.id or uuid.uuid4()
        obj.created_at = obj.created_at or NOW
        obj.updated_at = obj.updated_at or NOW
        obj.is_hidden = getattr(obj, "is_hidden", False)
        obj.is_invalid = getattr(obj, "is_invalid", False)
        obj.invalid_reason = getattr(obj, "invalid_reason", None)

    mock_db.refresh = AsyncMock(side_effect=_refresh)
    return mock_db


@pytest.mark.asyncio
async def test_create_ytd_variant_without_hierarchy_rejected(client):
    """ytd variant requires a time hierarchy linked to the base measure.
    Without one, the endpoint must return 422."""
    col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    base_id = uuid.uuid4()
    base = types.SimpleNamespace(
        id=base_id,
        model_id=TEST_MODEL_ID,
        name="revenue",
        measure_type="standard",
        variant_kind=None,
        calendar_model_table_id=None,
    )
    mock_db = _make_db_for_variant(col_id, table_id, base_measure=base)

    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.post(PREFIX, json={
            "name": "revenue_ytd",
            "source_table_id": str(table_id),
            "source_column_name": "amount",
            "measure_type": "standard",
            "variant_kind": "ytd",
            "variant_of_measure_id": str(base_id),
        })

    assert resp.status_code == 422, resp.text
    assert "hierarchy" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_variant_of_calculated_measure_rejected(client):
    """Time variants of calculated measures are not supported — 422."""
    col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    base_id = uuid.uuid4()
    base = types.SimpleNamespace(
        id=base_id,
        model_id=TEST_MODEL_ID,
        name="margin",
        measure_type="calculated",
        variant_kind=None,
        calendar_model_table_id=None,
    )
    mock_db = _make_db_for_variant(col_id, table_id, base_measure=base)

    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.post(PREFIX, json={
            "name": "margin_ytd",
            "source_table_id": str(table_id),
            "source_column_name": "amount",
            "measure_type": "standard",
            "variant_kind": "ytd",
            "variant_of_measure_id": str(base_id),
        })

    assert resp.status_code == 422, resp.text
    assert "calculated" in resp.json()["detail"].lower()


class TestSemiAdditiveVariantEligibility:
    """Bug-6222 (F-015-27): cumulation/window variants of semi-additive
    measures must be rejected at admission.

    Known value: an account balance measure with semi_additive_behavior=
    last_non_empty has monthly balances 100 (Jan), 110 (Feb), 120 (Mar).
    ``balance_ytd`` using SUM OVER returns 100, 210, 330 -- the WRONG
    answer. The correct YTD of a balance is 100, 110, 120 (the balance
    itself, since it is already a cumulative position). Rather than emit
    wrong numbers, the admission gate must reject these variants."""

    def test_variant_reason_rejects_ytd_on_semi_additive(self):
        """YTD (period_to_date family) must be rejected for semi-additive
        measures."""
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            "ytd",
            has_hierarchy=True,
            units={"year", "quarter", "month"},
            calcs={"period_to_date", "parallel_period", "moving_window", "lag"},
            has_calendar_rules=True,
            semi_additive_behavior="last_non_empty",
        )
        assert reason is not None
        assert "semi-additive" in reason.lower()
        assert "cumulation" in reason.lower() or "not supported" in reason.lower()

    @pytest.mark.parametrize("kind", ["ytd", "qtd", "mtd", "wtd", "ytd_prior_year"])
    def test_all_ptd_variants_rejected_on_semi_additive(self, kind):
        """All period-to-date variants must be blocked on balance measures."""
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            kind,
            has_hierarchy=True,
            units={"year", "quarter", "month", "week"},
            calcs={"period_to_date", "parallel_period", "moving_window", "lag"},
            has_calendar_rules=True,
            semi_additive_behavior="last_non_empty",
        )
        assert reason is not None, f"{kind} should be ineligible for semi-additive"

    @pytest.mark.parametrize("kind", ["trailing_n", "moving_avg_n"])
    def test_window_variants_rejected_on_semi_additive(self, kind):
        """Moving-window variants must be blocked on balance measures.
        trailing_n of a balance would sum 3 months of balances, giving
        a meaningless total. moving_avg_n would average them -- also wrong
        because balances are not additive across periods."""
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            kind,
            has_hierarchy=True,
            units={"year", "quarter", "month", "week"},
            calcs={"period_to_date", "parallel_period", "moving_window", "lag"},
            has_calendar_rules=True,
            semi_additive_behavior="last_non_empty",
            has_date_column=True,
        )
        assert reason is not None, f"{kind} should be ineligible for semi-additive"
        assert "semi-additive" in reason.lower()

    @pytest.mark.parametrize("kind", ["prior_year", "prior_quarter", "prior_month"])
    def test_prior_variants_allowed_on_semi_additive(self, kind):
        """Prior-period (parallel_period family) variants ARE meaningful for
        balances: prior_year of a balance is 'what was the balance last year at
        this point', which is a valid comparison. These use the hierarchy path,
        not a window date column."""
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            kind,
            has_hierarchy=True,
            units={"year", "quarter", "month", "week"},
            calcs={"period_to_date", "parallel_period", "moving_window", "lag"},
            has_calendar_rules=True,
            semi_additive_behavior="last_non_empty",
        )
        assert reason is None, f"{kind} should be eligible for semi-additive"

    def test_lag_allowed_on_semi_additive_with_date_column(self):
        """F-015-01/02: lag is a WINDOW variant — it orders by a date column,
        not the hierarchy. It is meaningful for balances (last year's balance
        at this point) and admissible when a date column is available."""
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            "lag",
            has_hierarchy=True,
            units={"year", "quarter", "month", "week"},
            calcs={"period_to_date", "parallel_period", "moving_window", "lag"},
            has_calendar_rules=True,
            semi_additive_behavior="last_non_empty",
            has_date_column=True,
        )
        assert reason is None

    def test_non_semi_additive_measure_ytd_still_allowed(self):
        """Regular (additive) measures must still admit YTD -- no
        regression from the semi-additive gate."""
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            "ytd",
            has_hierarchy=True,
            units={"year", "quarter", "month"},
            calcs={"period_to_date", "parallel_period", "moving_window", "lag"},
            has_calendar_rules=True,
            semi_additive_behavior=None,
        )
        assert reason is None


class TestWindowVariantCalendarFreeEligibility:
    """F-015-02: window variants (lag / trailing_n / moving_avg_n) must be
    admissible with NO hierarchy and NO calendar, given a date column to order
    by. The old shared precondition rejected every variant when no hierarchy
    existed, breaking the advertised calendar-free rolling-measure workflow."""

    @pytest.mark.parametrize("kind", ["lag", "trailing_n", "moving_avg_n"])
    def test_window_variant_admissible_without_hierarchy(self, kind):
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            kind,
            has_hierarchy=False,       # no hierarchy at all
            units=set(),
            calcs=set(),
            has_calendar_rules=False,  # no calendar
            semi_additive_behavior=None,
            has_date_column=True,      # but a date column exists
        )
        assert reason is None, f"{kind} should be admissible without a hierarchy"

    @pytest.mark.parametrize("kind", ["lag", "trailing_n", "moving_avg_n"])
    def test_window_variant_rejected_without_date_column(self, kind):
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            kind,
            has_hierarchy=False,
            units=set(),
            calcs=set(),
            has_calendar_rules=False,
            semi_additive_behavior=None,
            has_date_column=False,
        )
        assert reason is not None
        assert "date" in reason.lower()

    @pytest.mark.parametrize("kind", ["ytd", "prior_year"])
    def test_period_aware_still_needs_hierarchy(self, kind):
        """Period-aware variants must STILL require a hierarchy — the window
        carve-out must not leak to them."""
        from src.api.measures import _variant_reason
        reason = _variant_reason(
            kind,
            has_hierarchy=False,
            units=set(),
            calcs=set(),
            has_calendar_rules=False,
            semi_additive_behavior=None,
            has_date_column=True,
        )
        assert reason is not None
        assert "hierarchy" in reason.lower()
