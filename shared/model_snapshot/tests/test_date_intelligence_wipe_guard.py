"""Bug-5348 — the rehydrator must refuse to silently wipe a model's date
intelligence when a legacy snapshot omits the 'hierarchies' key, while still
allowing an explicit empty list (a deliberate clear) and normal snapshots."""
import uuid

import pytest

from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    _guard_date_intelligence_wipe,
)

MID = uuid.uuid4()


def test_key_absent_with_existing_hierarchies_aborts():
    with pytest.raises(SnapshotSchemaError) as exc:
        _guard_date_intelligence_wipe({}, MID, has_existing_hierarchies=True)
    assert "hierarchies" in str(exc.value)
    assert str(MID) in str(exc.value)


def test_key_absent_but_no_existing_hierarchies_is_allowed():
    # Fresh model (nothing to lose) — rehydration proceeds.
    _guard_date_intelligence_wipe({}, MID, has_existing_hierarchies=False)


def test_explicit_empty_list_is_a_deliberate_clear():
    # Key present (empty) = the operator chose to clear — allowed even with
    # existing hierarchies.
    _guard_date_intelligence_wipe(
        {"hierarchies": []}, MID, has_existing_hierarchies=True
    )


def test_normal_snapshot_with_hierarchies_is_allowed():
    _guard_date_intelligence_wipe(
        {"hierarchies": [{"name": "Calendar"}]}, MID, has_existing_hierarchies=True
    )
