"""Contract guard: MeasureResponse mirrors the stored resolved calendar snapshot.

Regression for the bug where `_build_response` populated variant_kind /
calendar_model_table_id / hierarchy_id but never copied resolved_calendar_id or
resolved_date_col_id, so the API always reported them null even when the DB row
had them set. Test escape: no prior test asserted these two response fields
against the stored ORM values. Guard: T1 contract test on the shared builder.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _stub_measure(**overrides):
    """A minimal stored-measure stand-in with every attribute `_build_response`
    reads. `_build_response` uses direct attribute access for required fields
    and getattr(..., None) for optional ones, so all must be present."""
    base = dict(
        id=uuid4(),
        model_id=uuid4(),
        name="base_amount_ytd_prior_year_retail",
        display_name="Base Amount YTD PY (Retail)",
        description=None,
        display_folder=None,
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type="base",
        expression=None,
        calc_agg_mode=None,
        data_type="decimal",
        default_agg="sum",
        format=None,
        variant_kind="ytd",
        variant_of_measure_id=uuid4(),
        variant_n=None,
        is_additive=True,
        semi_additive_behavior=None,
        semi_additive_account_column_id=None,
        calendar_model_table_id=uuid4(),
        hierarchy_id=uuid4(),
        date_dimension_column_id=None,
        resolved_calendar_id=uuid4(),
        resolved_date_col_id=uuid4(),
        is_invalid=False,
        invalid_reason=None,
        cross_model_source_model_id=None,
        cross_model_source_measure_id=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_build_response_mirrors_resolved_calendar_snapshot():
    """For a table-bound variant, the response must echo the stored
    resolved_calendar_id and resolved_date_col_id verbatim (not null)."""
    from unittest.mock import AsyncMock

    from src.api.measures import _build_response

    measure = _stub_measure()
    db = AsyncMock()

    # Empty glossary map avoids any DB round-trip in the builder.
    resp = await _build_response(db, measure, glossary_texts={})

    assert resp.resolved_calendar_id == measure.resolved_calendar_id
    assert resp.resolved_date_col_id == measure.resolved_date_col_id


@pytest.mark.asyncio
async def test_build_response_preserves_null_resolved_fields():
    """A measure with no resolved snapshot serialises both fields as null."""
    from unittest.mock import AsyncMock

    from src.api.measures import _build_response

    measure = _stub_measure(
        variant_kind=None,
        variant_of_measure_id=None,
        calendar_model_table_id=None,
        hierarchy_id=None,
        resolved_calendar_id=None,
        resolved_date_col_id=None,
    )
    db = AsyncMock()

    resp = await _build_response(db, measure, glossary_texts={})

    assert resp.resolved_calendar_id is None
    assert resp.resolved_date_col_id is None
