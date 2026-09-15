"""Bug-9265 — an unresolved variant BASE must fail closed, not fall through.

``object_touches_restricted`` documents a single invariant: "Fail-closed on any
unverifiable branch." The calculated-measure branch honours it — an unresolved
``measure("name")`` reference returns restricted, because a filtered list, a
rename or draft/deploy skew all make the reference unprovable.

The variant branch did not. When a variant measure's base was absent from the
closure context it simply fell through and the object was reported UNRESTRICTED,
hiding whatever the base's own lineage reached. That is the more dangerous
direction of the two: the caller believes the closure was checked.

This matters more, not less, once the closure is pinned to a deployed snapshot
(Bug-9490): a base missing from the pinned universe becomes a normal condition
rather than an anomaly, so the branch must already be safe.
"""
from __future__ import annotations

import pytest

from types import SimpleNamespace

from shared.security.restricted_column_closure import (
    ClosureContext,
    object_touches_restricted,
)

RESTRICTED_COL = "col-restricted"
CLEAN_COL = "col-clean"


def _measure(**kw):
    base = dict(
        id="m1", name="m1", source_column_id=None, display_column_id=None,
        user_defined_attribute_id=None, variant_of_measure_id=None,
        measure_type="base", expression=None, calc_expression=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_bug9265_missing_variant_base_is_restricted():
    """THE fix. An absent base is unverifiable, so the variant is restricted."""
    variant = _measure(id="v1", name="v1", variant_of_measure_id="missing-base")
    ctx = ClosureContext(measures_by_id={}, measure_universe_complete=True)  # base not in the universe
    assert object_touches_restricted(variant, {RESTRICTED_COL}, ctx) is True


def test_bug9265_present_clean_base_is_not_restricted():
    """The fix must not over-block a base that IS resolvable and clean."""
    base = _measure(id="b1", name="b1", source_column_id=CLEAN_COL)
    variant = _measure(id="v1", name="v1", variant_of_measure_id="b1")
    ctx = ClosureContext(measures_by_id={"b1": base}, measure_universe_complete=True)
    assert object_touches_restricted(variant, {RESTRICTED_COL}, ctx) is False


def test_bug9265_present_restricted_base_is_restricted():
    base = _measure(id="b1", name="b1", source_column_id=RESTRICTED_COL)
    variant = _measure(id="v1", name="v1", variant_of_measure_id="b1")
    ctx = ClosureContext(measures_by_id={"b1": base}, measure_universe_complete=True)
    assert object_touches_restricted(variant, {RESTRICTED_COL}, ctx) is True


def test_bug9265_variant_chain_resolves_through_two_levels():
    root = _measure(id="b0", name="b0", source_column_id=RESTRICTED_COL)
    mid = _measure(id="b1", name="b1", variant_of_measure_id="b0")
    top = _measure(id="v1", name="v1", variant_of_measure_id="b1")
    ctx = ClosureContext(measures_by_id={"b0": root, "b1": mid}, measure_universe_complete=True)
    assert object_touches_restricted(top, {RESTRICTED_COL}, ctx) is True


def test_bug9265_broken_chain_midway_is_restricted():
    """A chain that cannot be walked to the end is unverifiable."""
    mid = _measure(id="b1", name="b1", variant_of_measure_id="b0-missing")
    top = _measure(id="v1", name="v1", variant_of_measure_id="b1")
    ctx = ClosureContext(measures_by_id={"b1": mid}, measure_universe_complete=True)
    assert object_touches_restricted(top, {RESTRICTED_COL}, ctx) is True


def test_bug9265_self_referential_variant_terminates():
    """The cycle guard must still stop, and must not be defeated by the fix."""
    loop = _measure(id="v1", name="v1", variant_of_measure_id="v1")
    ctx = ClosureContext(measures_by_id={"v1": loop}, measure_universe_complete=True)
    assert object_touches_restricted(loop, {RESTRICTED_COL}, ctx) is False


def test_bug9265_an_incomplete_context_is_refused_not_guessed():
    """Consolidation: the primitive will not answer from a partial context.

    Returning "restricted" for a dependency-bearing object whose universe was
    never loaded looked like fail-closed safety. It was not: it hid clean
    variants across the measures API the moment any column was restricted. The
    caller error now surfaces where it is written.
    """
    from shared.security.restricted_column_closure import IncompleteClosureContext

    variant = _measure(id="v1", name="v1", variant_of_measure_id="b1")
    with pytest.raises(IncompleteClosureContext):
        object_touches_restricted(variant, {RESTRICTED_COL}, ClosureContext())

    calc = _measure(
        id="c1", name="c1", measure_type="calculated", expression='measure("x")',
    )
    with pytest.raises(IncompleteClosureContext):
        object_touches_restricted(calc, {RESTRICTED_COL}, ClosureContext())


def test_bug9265_a_plain_measure_needs_no_universe():
    """No dependency, no requirement — the common path stays cheap."""
    plain = _measure(id="p1", name="p1", source_column_id=RESTRICTED_COL)
    assert object_touches_restricted(plain, {RESTRICTED_COL}, ClosureContext()) is True
