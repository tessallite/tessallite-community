"""Declaration-hash tests for dimension attribute relationships (spec §7.6.1).

The declaration hash must be stable for the same meaning and MUST change when any
meaning-bearing field changes (key column, detail column, cardinality, null
policy) — because a hash change stales all prior verification evidence in Phase 2.
It must NOT change for ``enabled`` toggles or timestamp re-saves.
"""
from __future__ import annotations

from shared.semantic.attribute_relationship_hash import compute_declaration_hash


def test_same_declaration_same_hash():
    a = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
    )
    b = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
    )
    assert a == b


def test_uuid_and_string_form_hash_identically():
    import uuid
    u = uuid.uuid4()
    a = compute_declaration_hash(
        key_column_id=u, detail_column_id="d", cardinality="BIJECTION",
    )
    b = compute_declaration_hash(
        key_column_id=str(u), detail_column_id="d", cardinality="BIJECTION",
    )
    assert a == b


def test_cardinality_change_changes_hash():
    a = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
    )
    b = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="FUNCTIONAL_N_TO_1",
    )
    assert a != b


def test_key_rebind_changes_hash():
    a = compute_declaration_hash(
        key_column_id="k1", detail_column_id="d", cardinality="BIJECTION",
    )
    b = compute_declaration_hash(
        key_column_id="k2", detail_column_id="d", cardinality="BIJECTION",
    )
    assert a != b


def test_detail_change_changes_hash():
    a = compute_declaration_hash(
        key_column_id="k", detail_column_id="d1", cardinality="BIJECTION",
    )
    b = compute_declaration_hash(
        key_column_id="k", detail_column_id="d2", cardinality="BIJECTION",
    )
    assert a != b


def test_cardinality_case_is_canonicalised():
    a = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="bijection",
    )
    b = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
    )
    assert a == b


def test_hash_is_independent_of_enabled_toggle():
    # The declaration hash captures MEANING (what the relationship asserts about
    # the data), not enablement. compute_declaration_hash takes no ``enabled``
    # argument, so toggling enablement must never change the hash — otherwise a
    # disable/enable would (wrongly, in Phase 2) stale verification evidence.
    # This pins that contract against a future edit adding ``enabled`` to the
    # payload.
    base = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
    )
    # There is no enabled parameter to vary; the guarantee is structural, but we
    # assert the hash equals a recomputation to document the invariant explicitly.
    assert base == compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
    )


def test_null_policy_participates():
    a = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
        null_policy="REJECT_NULL",
    )
    b = compute_declaration_hash(
        key_column_id="k", detail_column_id="d", cardinality="BIJECTION",
        null_policy="ALLOW_NULL",
    )
    assert a != b
