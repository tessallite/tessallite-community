"""Bug-9265 (review finding 3) — a clean variant must stay visible.

The shared closure now fails closed when a variant's BASE cannot be found in the
measure universe: an unresolvable base cannot be proven safe. That is right when
the universe is complete.

This caller's universe is deliberately EMPTY. ``_measure_touches_restricted_column``
is documented as "the direct seed, so no measure map is supplied here" — transitive
hiding is computed separately. With the map empty, every variant measure looked
unresolvable, so the moment a persona restricted ANY column anywhere in the model,
a variant built on a completely unrelated clean measure was reported restricted
and its metadata endpoint returned 404.

That is a product regression, not a security improvement: the primitive was asked
for an authoritative answer from a context the caller knew was incomplete. The
fix supplies the universe rather than relaxing the rule.
"""
from __future__ import annotations

import uuid

import pytest
from shared.security.restricted_column_closure import (
    IncompleteClosureContext,
)

from src.api.measures import _measure_touches_restricted_column


class _M:
    def __init__(self, **kw):
        self.id = kw.get("id") or uuid.uuid4()
        self.name = kw.get("name", "m")
        self.source_column_id = kw.get("source_column_id")
        self.display_column_id = None
        self.user_defined_attribute_id = kw.get("user_defined_attribute_id")
        self.variant_of_measure_id = kw.get("variant_of_measure_id")
        self.measure_type = kw.get("measure_type", "base")
        self.expression = kw.get("expression")
        self.calc_expression = None


def test_bug9265_clean_variant_is_not_hidden_by_an_unrelated_restriction():
    """THE regression, checked the way the endpoint now calls it.

    The endpoint loads the measure universe for a dependency-bearing object
    before asking, so the base resolves and the clean variant stays visible.
    """
    restricted = uuid.uuid4()
    clean_col = uuid.uuid4()
    base = _M(name="revenue", source_column_id=clean_col)
    variant = _M(
        name="revenue_ytd",
        source_column_id=clean_col,          # modern variants copy the source
        variant_of_measure_id=base.id,
    )
    assert _measure_touches_restricted_column(
        variant, {restricted}, None, [base, variant],
    ) is False, (
        "a variant on a clean base was hidden despite a complete universe"
    )


def test_bug9265_primitive_refuses_to_answer_without_a_universe():
    """The shared rule is NOT relaxed — the caller must supply the context.

    This asserted a fail-closed RETURN of True until the consolidation that
    made closure-context completeness explicit. The primitive now RAISES
    ``IncompleteClosureContext`` instead, which is the stronger form of the same
    property: a silent True is an answer, and an answer can be cached, inverted
    by a later refactor, or read as "restricted" when the truth is "unknown".
    An exception cannot be mistaken for a judgement, and it surfaces at the
    caller that omitted the universe rather than one layer down.

    That distinction is the whole of Bug-9265: the silent fail-closed answer is
    what hid clean variants from personas entitled to see them.
    """
    restricted = uuid.uuid4()
    variant = _M(name="revenue_ytd", variant_of_measure_id=uuid.uuid4())
    with pytest.raises(IncompleteClosureContext):
        _measure_touches_restricted_column(variant, {restricted})


def test_bug9265_a_variant_on_a_restricted_base_is_still_hidden():
    """The security property must survive the correction."""
    restricted = uuid.uuid4()
    base = _M(name="salary", source_column_id=restricted)
    variant = _M(
        name="salary_ytd",
        source_column_id=restricted,
        variant_of_measure_id=base.id,
    )
    assert _measure_touches_restricted_column(variant, {restricted}) is True


def test_bug9265_no_restriction_hides_nothing():
    base = _M(name="revenue", source_column_id=uuid.uuid4())
    variant = _M(name="revenue_ytd", variant_of_measure_id=base.id)
    assert _measure_touches_restricted_column(variant, set()) is False


def test_bug9265_universe_is_loaded_only_for_dependency_bearing_measures():
    """Cost stays where correctness needs it.

    Most measures carry no dependency, so the single-measure read must not pay
    for a whole-model load. Asserted on the helper the endpoints call.
    """
    import asyncio

    from src.api.measures import _dependency_measure_universe

    plain = _M(name="revenue", source_column_id=uuid.uuid4())
    variant = _M(name="revenue_ytd", variant_of_measure_id=uuid.uuid4())
    calc = _M(name="margin", measure_type="calculated")

    async def _never_called(*_a, **_kw):
        raise AssertionError("loaded the measure universe when it was not needed")

    class _DB:
        execute = _never_called

    # No dependency -> no load, whatever the restriction state.
    assert asyncio.run(
        _dependency_measure_universe(_DB(), uuid.uuid4(), plain, {uuid.uuid4()})
    ) is None
    # No restriction -> no load even for a dependency-bearing measure.
    assert asyncio.run(
        _dependency_measure_universe(_DB(), uuid.uuid4(), variant, set())
    ) is None
    assert asyncio.run(
        _dependency_measure_universe(_DB(), uuid.uuid4(), calc, set())
    ) is None
