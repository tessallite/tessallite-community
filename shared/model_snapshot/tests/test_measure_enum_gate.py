"""F-020-09: the rehydrator enum gate must reject invalid measure enums.

Importers historically wrote invalid semi_additive_behavior strings (e.g.
"last_value", "max_over_order_date") that bypassed the Pydantic API layer
because the rehydrator inserts rows directly. The gate rejects them at the
snapshot boundary so they never reach the query rewriter.
"""
import pytest

from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    _validate_measure_enums,
)


def test_valid_semi_additive_passes():
    _validate_measure_enums({"name": "m", "semi_additive_behavior": "last_non_empty"})
    _validate_measure_enums({"name": "m", "semi_additive_behavior": None})
    _validate_measure_enums({"name": "m"})  # absent key is fine


@pytest.mark.parametrize(
    "bad",
    ["last_value", "max_over_order_date", "first_value", "garbage"],
)
def test_invalid_semi_additive_rejected(bad):
    with pytest.raises(SnapshotSchemaError) as exc:
        _validate_measure_enums(
            {"name": "revenue", "semi_additive_behavior": bad}
        )
    assert "semi_additive_behavior" in str(exc.value)
    assert "revenue" in str(exc.value)
