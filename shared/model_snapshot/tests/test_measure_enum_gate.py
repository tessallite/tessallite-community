"""Rehydrator measure-enum boundary: strict-forward / tolerant-read.

Importers historically wrote non-canonical default_agg ("average"/"median") and
semi_additive_behavior ("last_value"/"max_over_order_date") tokens that bypass
the Pydantic API layer because the rehydrator inserts rows directly. The FORWARD
create/update API surface rejects such values (strict). The REHYDRATE/IMPORT
boundary is tolerant so a legacy saved version or exported bundle is never
bricked by an enum tightening (Bug-6591 default_agg, Bug-6613
semi_additive_behavior): a known legacy token is read-coerced to its canonical
form; an unresolvable token imports as a DISABLED measure (safe default +
is_invalid + reason), never a hard reject.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    _insert_measures,
    _validate_measure_enums,
)


def test_valid_semi_additive_passes():
    _validate_measure_enums({"name": "m", "semi_additive_behavior": "last_non_empty"})
    _validate_measure_enums({"name": "m", "semi_additive_behavior": None})
    _validate_measure_enums({"name": "m"})  # absent key is fine


# Bug-6613 (sibling of Bug-6591): the rehydrate/import boundary must READ-COERCE
# legacy non-canonical ``semi_additive_behavior`` tokens, NOT hard-reject them.
# The old AtScale mapper emitted f"{position}_value" (e.g. "last_value",
# "max_value") — invalid enums that still live inside every model version and
# exported bundle persisted before the F-020-09 mapper fix. A hard reject made
# every such saved snapshot un-revertable/un-importable — the exact Bug-6591
# failure mode on a different field. Test escape: the old reject-gate test below
# locked the regression in. Guard: coercion is now proven for the known legacy
# tokens and the disabled-measure fallback for unresolvable ones.
@pytest.mark.parametrize(
    "legacy,canonical",
    [
        ("last_value", "last_non_empty"),
        ("first_value", "first_non_empty"),
        ("min_value", "min"),
        ("max_value", "max"),
        ("average_value", "avg_of_children"),
        ("LAST_VALUE", "last_non_empty"),  # case-normalised
        ("Max_Value", "max"),
    ],
)
def test_legacy_semi_additive_coerced_to_canonical(legacy, canonical):
    row = {"name": "balance", "semi_additive_behavior": legacy}
    _validate_measure_enums(row)  # must NOT raise — legacy snapshot must revert/import
    assert row["semi_additive_behavior"] == canonical
    # a straight coercion does not disable the measure
    assert not row.get("is_invalid")


@pytest.mark.parametrize(
    "unresolvable",
    ["max_over_order_date", "garbage", "last_value_of_snapshot"],
)
def test_unresolvable_semi_additive_imports_as_disabled_measure(unresolvable):
    """A free-form token (e.g. "max_over_order_date", which embeds a column name)
    cannot resolve to a canonical enum. Rather than bricking the whole snapshot,
    the measure imports DISABLED: semi_additive_behavior reset to NULL (fully
    additive — the mapper's unknown-position fallback) and the measure flagged
    invalid with a reason. Nulling rather than guessing avoids WRONG NUMBERS
    (a balance whose intended last-non-empty behaviour we cannot recover must
    not silently SUM across time). The invalid flag + reason are the operator
    signal to set the behaviour and re-enable it."""
    row = {"name": "balance", "semi_additive_behavior": unresolvable}
    _validate_measure_enums(row)  # must NOT raise
    assert row["semi_additive_behavior"] is None
    assert row["is_invalid"] is True
    assert row["invalid_reason"]
    assert unresolvable in row["invalid_reason"]


@pytest.mark.parametrize(
    "canonical",
    ["min", "max", "last_non_empty", "first_non_empty", "avg_of_children"],
)
def test_canonical_semi_additive_passes_through_uncoerced(canonical):
    """Guards the "nothing canonical is caught by the legacy map" property. The
    only canonical values whose stems collide with a legacy key are min/max (vs
    min_value/max_value); assert they (and the rest) pass through un-coerced and
    un-disabled. ``by_account`` is deliberately NOT in this list any more — #10
    removed it (see the disabled-import test below)."""
    row = {"name": "m", "semi_additive_behavior": canonical}
    _validate_measure_enums(row)
    assert row["semi_additive_behavior"] == canonical
    assert not row.get("is_invalid")


@pytest.mark.parametrize("token", ["by_account", "BY_ACCOUNT", "By_Account"])
def test_by_account_imports_as_a_disabled_measure(token):
    """#10: ``by_account`` is no longer a supported behaviour. A snapshot/import
    still carrying it must NOT brick the snapshot and must NOT persist the token:
    it takes the existing unresolvable-token path (behaviour NULLed, is_invalid +
    reason), the same mechanism migration 0215 applies to already-live rows."""
    row = {"name": "balance", "semi_additive_behavior": token}
    _validate_measure_enums(row)  # must NOT raise
    assert row["semi_additive_behavior"] is None
    assert row["is_invalid"] is True
    assert row["invalid_reason"]


def test_valid_semi_additive_normalises_case():
    row = {"name": "m", "semi_additive_behavior": "LAST_NON_EMPTY"}
    _validate_measure_enums(row)
    assert row["semi_additive_behavior"] == "last_non_empty"
    assert not row.get("is_invalid")


def test_semi_additive_disabled_fallback_preserves_existing_invalid_reason():
    """If the measure was already invalid for another reason, the semi-additive
    coercion does not clobber that reason."""
    row = {
        "name": "balance",
        "semi_additive_behavior": "max_over_order_date",
        "is_invalid": True,
        "invalid_reason": "original reason",
    }
    _validate_measure_enums(row)
    assert row["semi_additive_behavior"] is None
    assert row["invalid_reason"] == "original reason"


def test_forward_api_validation_still_rejects_bad_semi_additive():
    """Producer/consumer alignment: the REHYDRATE boundary is now tolerant, but
    the FORWARD create API surface must stay strict — a raw-API caller still
    cannot persist a non-canonical semi_additive_behavior."""
    from shared.schemas.domains.dimensions_measures import MeasureCreate

    # #10: ``by_account`` is now among the rejected values — a caller must not be
    # able to AUTHOR a new by_account measure, even with an account column.
    for bad in ["last_value", "max_over_order_date", "garbage", "by_account"]:
        with pytest.raises(ValueError):
            MeasureCreate(name="m", semi_additive_behavior=bad)
    import uuid as _uuid
    with pytest.raises(ValueError):
        MeasureCreate(
            name="m",
            semi_additive_behavior="by_account",
            semi_additive_account_column_id=_uuid.uuid4(),
        )
    # a canonical value passes
    ok = MeasureCreate(name="m", semi_additive_behavior="last_non_empty")
    assert ok.semi_additive_behavior == "last_non_empty"


def test_valid_default_agg_passes_and_normalises():
    row = {"name": "m", "default_agg": "sum"}
    _validate_measure_enums(row)
    assert row["default_agg"] == "sum"
    # canonical quantile stat (median -> p50) is accepted
    _validate_measure_enums({"name": "m", "default_agg": "p50"})
    # case is normalised in place to match the persisted convention
    row = {"name": "m", "default_agg": "AVG"}
    _validate_measure_enums(row)
    assert row["default_agg"] == "avg"
    _validate_measure_enums({"name": "m", "default_agg": None})  # absent/None fine


# Bug-6591 (REOPENED): the rehydrate/import boundary must READ-COERCE legacy
# non-canonical default_agg tokens, NOT hard-reject them. An earlier fix rejected
# "average"/"median"/"percentile" here to mirror the API surface — but legacy
# snapshots persisted BEFORE the mapper normalisation carry those exact values,
# so hard reject bricked every such saved version and exported bundle
# (un-revertable, un-importable). Test escape: the reject-gate test locked the
# regression in. Guard: coercion is now proven for the known legacy tokens and
# the disabled-measure fallback for unresolvable ones.
@pytest.mark.parametrize(
    "legacy,canonical",
    [("average", "avg"), ("median", "p50"), ("AVERAGE", "avg"), ("Median", "p50")],
)
def test_legacy_default_agg_coerced_to_canonical(legacy, canonical):
    row = {"name": "revenue", "default_agg": legacy}
    _validate_measure_enums(row)  # must NOT raise — legacy snapshot must revert/import
    assert row["default_agg"] == canonical
    # a straight coercion does not disable the measure
    assert not row.get("is_invalid")


@pytest.mark.parametrize("unresolvable", ["percentile", "total", "stddev", "gibberish"])
def test_unresolvable_default_agg_imports_as_disabled_measure(unresolvable):
    """A bare 'percentile' (no fraction) or any unknown token cannot resolve to a
    canonical aggregate. Rather than bricking the whole snapshot, the measure is
    imported DISABLED: default_agg reset to the NOT-NULL column default and the
    measure flagged invalid with a reason. This matches what the live importers
    persist for an unrepresentable aggregate (atscale_mapper._resolve_calc_method
    also returns "sum" + is_invalid), so the rehydrate and mapper boundaries
    agree. The invalid flag + reason are the operator signal to re-enable it."""
    row = {"name": "revenue", "default_agg": unresolvable}
    _validate_measure_enums(row)  # must NOT raise
    assert row["default_agg"] == "sum"
    assert row["is_invalid"] is True
    assert row["invalid_reason"]
    assert unresolvable in row["invalid_reason"]


def test_disabled_fallback_preserves_existing_invalid_reason():
    """If the measure was already invalid for another reason, the coercion does
    not clobber that reason."""
    row = {
        "name": "revenue",
        "default_agg": "total",
        "is_invalid": True,
        "invalid_reason": "original reason",
    }
    _validate_measure_enums(row)
    assert row["default_agg"] == "sum"
    assert row["invalid_reason"] == "original reason"


def test_forward_api_validation_still_rejects_bad_default_agg():
    """Producer/consumer alignment: the REHYDRATE boundary is now tolerant, but
    create stays strict and update accepts only known legacy synonyms. A raw-API
    caller still cannot persist an unknown default_agg."""
    from shared.schemas.domains.dimensions_measures import (
        MeasureCreate,
        MeasureUpdate,
        _validate_default_agg,
    )

    for bad in ["average", "median", "percentile", "total"]:
        with pytest.raises(ValueError):
            _validate_default_agg(bad)
        with pytest.raises(ValueError):
            MeasureCreate(name="m", default_agg=bad)
    # canonical values still pass and normalise
    assert _validate_default_agg("AVG") == "avg"
    assert _validate_default_agg("p50") == "p50"
    assert MeasureUpdate(default_agg="average").default_agg == "avg"
    assert MeasureUpdate(default_agg="median").default_agg == "p50"
    assert MeasureUpdate(default_agg="AVERAGE").default_agg == "avg"
    for bad in ["percentile", "total"]:
        with pytest.raises(ValueError):
            MeasureUpdate(default_agg=bad)


# ---------------------------------------------------------------------------
# Integration: the whole rehydrate insert path (not just the unit gate) must
# coerce a legacy measure so a legacy snapshot inserts cleanly (Bug-6591).
# ---------------------------------------------------------------------------

def _capture_measure_inserts():
    """Mock AsyncSession that records the params of every Measure insert."""
    db = AsyncMock()
    inserts: list[dict] = []

    async def _execute(stmt):
        try:
            params = dict(stmt.compile().params)
        except Exception:
            params = {}
        inserts.append(params)
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._inserts = inserts
    return db


@pytest.mark.asyncio
async def test_insert_measures_coerces_legacy_snapshot():
    """A legacy snapshot whose measures carry default_agg 'average'/'median'
    rehydrates without raising and persists the canonical token — the exact
    scenario the reopened Bug-6591 reject-gate bricked."""
    model_id = uuid.uuid4()
    snap = {
        "measures": [
            {"id": str(uuid.uuid4()), "name": "avg_price", "default_agg": "average"},
            {"id": str(uuid.uuid4()), "name": "median_price", "default_agg": "median"},
        ]
    }
    db = _capture_measure_inserts()
    await _insert_measures(model_id, snap, db)  # must NOT raise

    by_name = {p["name"]: p for p in db._inserts}
    assert by_name["avg_price"]["default_agg"] == "avg"
    assert by_name["median_price"]["default_agg"] == "p50"


@pytest.mark.asyncio
async def test_insert_measures_disables_unresolvable_legacy_measure():
    """A measure with an unresolvable legacy token imports disabled, not blocked."""
    model_id = uuid.uuid4()
    snap = {
        "measures": [
            {"id": str(uuid.uuid4()), "name": "p_thing", "default_agg": "percentile"},
        ]
    }
    db = _capture_measure_inserts()
    await _insert_measures(model_id, snap, db)  # must NOT raise

    row = db._inserts[0]
    assert row["default_agg"] == "sum"
    assert row["is_invalid"] is True


@pytest.mark.asyncio
async def test_insert_measures_coerces_legacy_semi_additive_snapshot():
    """Bug-6613: a legacy snapshot whose measures carry semi_additive_behavior
    'last_value'/'max_value' (the old AtScale f"{position}_value" form)
    rehydrates without raising and persists the canonical enum — the scenario
    the hard-reject gate bricked."""
    model_id = uuid.uuid4()
    snap = {
        "measures": [
            {"id": str(uuid.uuid4()), "name": "closing_balance",
             "semi_additive_behavior": "last_value"},
            {"id": str(uuid.uuid4()), "name": "peak_headcount",
             "semi_additive_behavior": "max_value"},
        ]
    }
    db = _capture_measure_inserts()
    await _insert_measures(model_id, snap, db)  # must NOT raise

    by_name = {p["name"]: p for p in db._inserts}
    assert by_name["closing_balance"]["semi_additive_behavior"] == "last_non_empty"
    assert by_name["peak_headcount"]["semi_additive_behavior"] == "max"


@pytest.mark.asyncio
async def test_insert_measures_disables_unresolvable_semi_additive():
    """A measure with a free-form semi_additive_behavior imports disabled (NULL
    behaviour + is_invalid), not blocked."""
    model_id = uuid.uuid4()
    snap = {
        "measures": [
            {"id": str(uuid.uuid4()), "name": "balance",
             "semi_additive_behavior": "max_over_order_date"},
        ]
    }
    db = _capture_measure_inserts()
    await _insert_measures(model_id, snap, db)  # must NOT raise

    row = db._inserts[0]
    # NULL semi_additive_behavior is dropped by _strip (falls to column default)
    assert row.get("semi_additive_behavior") is None
    assert row["is_invalid"] is True
