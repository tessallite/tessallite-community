"""F-024 — per-conversation model pin (fail-safe narrowing).

Unit tests for ``_apply_model_pin``, the pure helper that narrows the agent to a
single pinned model. The key invariant is fail-safe: a pin can only ever narrow
the allow-list, never widen it, so a stale or out-of-scope pin is ignored.
"""
import uuid

from src.prompt.assembler import _apply_model_pin, _ModelProfile


def _profile(model_id: uuid.UUID, slug: str) -> _ModelProfile:
    return _ModelProfile(
        id=model_id,
        slug=slug,
        display_name=slug.title(),
        overview=None,
        analytical_capabilities=None,
        abbreviation_conflict_rules=None,
        example_questions=[],
        measure_names=[],
        dimension_names=[],
        filterable_where_names=[],
        sortable_names=[],
        aggregates_summary=[],
        calendar_aliases=[],
        dimension_aliases=[],
        tagged_fields={},
        dimension_value_hints={},
    )


def test_pin_narrows_to_single_model():
    a, b = uuid.uuid4(), uuid.uuid4()
    profiles = [_profile(a, "sales"), _profile(b, "finance")]
    allow_ids = [a, b]
    primary = a

    new_profiles, new_allow, eff_primary = _apply_model_pin(profiles, allow_ids, primary, b)

    assert [p.id for p in new_profiles] == [b]
    assert new_allow == [b]
    # The pin overrides primary_model_id too.
    assert eff_primary == b


def test_no_pin_is_a_noop():
    a, b = uuid.uuid4(), uuid.uuid4()
    profiles = [_profile(a, "sales"), _profile(b, "finance")]
    allow_ids = [a, b]

    new_profiles, new_allow, eff_primary = _apply_model_pin(profiles, allow_ids, a, None)

    assert [p.id for p in new_profiles] == [a, b]
    assert new_allow == [a, b]
    assert eff_primary == a


def test_stale_pin_is_ignored_never_widens():
    """A pin not in the (already allow-list + persona filtered) allow_ids is
    silently dropped — the conversation degrades to the project default rather
    than gaining access to a model the caller is not entitled to."""
    a, b, gone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    profiles = [_profile(a, "sales"), _profile(b, "finance")]
    allow_ids = [a, b]

    new_profiles, new_allow, eff_primary = _apply_model_pin(profiles, allow_ids, a, gone)

    # Unchanged: the stale pin had no effect, access was not expanded.
    assert [p.id for p in new_profiles] == [a, b]
    assert new_allow == [a, b]
    assert eff_primary == a


def test_pin_out_of_persona_scope_is_ignored():
    """When persona filtering has already removed a model from allow_ids, a pin
    naming that model is treated as stale (out of scope) and ignored."""
    a, b = uuid.uuid4(), uuid.uuid4()
    # Persona filter left only `a` in scope; `b` was removed upstream.
    profiles = [_profile(a, "sales")]
    allow_ids = [a]

    new_profiles, new_allow, eff_primary = _apply_model_pin(profiles, allow_ids, a, b)

    assert [p.id for p in new_profiles] == [a]
    assert new_allow == [a]
    assert eff_primary == a
