"""Tests for cross-model measure detection and schema validation."""
from uuid import uuid4
import pytest
from src.ir.logical_query import CrossModelNotResolvedError


# ---------------------------------------------------------------------------
# CrossModelNotResolvedError
# ---------------------------------------------------------------------------

def test_error_message_contains_slug():
    err = CrossModelNotResolvedError("revenue", str(uuid4()))
    assert "revenue" in str(err)
    assert "cross-model" in str(err).lower()


def test_error_attributes():
    mid = str(uuid4())
    err = CrossModelNotResolvedError("cost", mid)
    assert err.measure_slug == "cost"
    assert err.source_model_id == mid


def test_error_message_mentions_same_project():
    err = CrossModelNotResolvedError("sales", str(uuid4()))
    # The error message should indicate same-project context
    msg = str(err)
    assert "sales" in msg
    assert "resolution is not yet supported" in msg


# ---------------------------------------------------------------------------
# MeasureCreate both-or-neither validator
# ---------------------------------------------------------------------------

def test_measure_create_both_cross_model_fields_accepted():
    from shared.schemas.pydantic_models import MeasureCreate
    m = MeasureCreate(
        name="test_measure",
        cross_model_source_model_id=uuid4(),
        cross_model_source_measure_id=uuid4(),
    )
    assert m.cross_model_source_model_id is not None
    assert m.cross_model_source_measure_id is not None


def test_measure_create_neither_cross_model_field_accepted():
    from shared.schemas.pydantic_models import MeasureCreate
    m = MeasureCreate(name="test_measure")
    assert m.cross_model_source_model_id is None
    assert m.cross_model_source_measure_id is None


def test_measure_create_only_model_id_raises():
    from shared.schemas.pydantic_models import MeasureCreate
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="both be set or both be null"):
        MeasureCreate(
            name="test_measure",
            cross_model_source_model_id=uuid4(),
            # cross_model_source_measure_id omitted
        )


def test_measure_create_only_measure_id_raises():
    from shared.schemas.pydantic_models import MeasureCreate
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="both be set or both be null"):
        MeasureCreate(
            name="test_measure",
            cross_model_source_measure_id=uuid4(),
            # cross_model_source_model_id omitted
        )


# ---------------------------------------------------------------------------
# MeasureUpdate both-or-neither validator
# ---------------------------------------------------------------------------

def test_measure_update_both_cross_model_fields_accepted():
    from shared.schemas.pydantic_models import MeasureUpdate
    m = MeasureUpdate(
        cross_model_source_model_id=uuid4(),
        cross_model_source_measure_id=uuid4(),
    )
    assert m.cross_model_source_model_id is not None


def test_measure_update_clear_both_accepted():
    from shared.schemas.pydantic_models import MeasureUpdate
    m = MeasureUpdate(
        cross_model_source_model_id=None,
        cross_model_source_measure_id=None,
    )
    assert m.cross_model_source_model_id is None
    assert m.cross_model_source_measure_id is None


def test_measure_update_only_model_id_raises():
    from shared.schemas.pydantic_models import MeasureUpdate
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="both be set or both be null"):
        MeasureUpdate(
            cross_model_source_model_id=uuid4(),
        )
