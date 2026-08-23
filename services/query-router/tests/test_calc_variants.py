"""Tests for the calculated-measure + time-variant interaction (F-015-16).

The previous version of this file constructed ad-hoc stub objects and
asserted the attributes it had just set — it exercised zero production code
(F-015-16). The real, shipping behaviour is that a *variant of a calculated
measure* cannot exist: the Pydantic schema rejects ``variant_kind`` on a
calculated measure at the API boundary, so no persisted row ever reaches the
calculated-variant branch in ``source_sql.py``. These tests pin that gate.
"""
import uuid

import pytest
from pydantic import ValidationError

from shared.schemas.domains.dimensions_measures import MeasureCreate, MeasureUpdate


def test_create_rejects_variant_kind_on_calculated_measure():
    """The schema gate that makes the calculated-variant rewrite branch
    unreachable: a calculated measure may not carry a variant_kind."""
    with pytest.raises(ValidationError) as exc:
        MeasureCreate(
            name="net_revenue_trailing_3",
            measure_type="calculated",
            expression="measure(\"revenue\") - measure(\"cost\")",
            calc_agg_mode="expression_as_written",
            variant_kind="trailing_n",
            variant_of_measure_id=uuid.uuid4(),
            variant_n=3,
        )
    assert "variants on calculated measures are not supported" in str(exc.value)


def test_create_allows_variant_on_standard_measure():
    """A standard measure CAN carry a variant_kind — the supported path."""
    body = MeasureCreate(
        name="revenue_trailing_3",
        measure_type="standard",
        source_table_id=uuid.uuid4(),
        source_column_name="revenue",
        variant_kind="trailing_n",
        variant_of_measure_id=uuid.uuid4(),
        variant_n=3,
    )
    assert body.variant_kind == "trailing_n"
    assert body.measure_type == "standard"


def test_update_cannot_carry_variant_kind():
    """The update schema has no ``variant_kind`` field at all, so a PATCH can
    never turn a measure into (or out of) a variant — the second arm of the
    gate that keeps the calculated-variant rewrite branch unreachable."""
    assert "variant_kind" not in MeasureUpdate.model_fields
    # A stray variant_kind in the body is ignored, not applied.
    body = MeasureUpdate.model_validate({"measure_type": "calculated", "variant_kind": "ytd"})
    assert not hasattr(body, "variant_kind") or getattr(body, "variant_kind", None) is None
