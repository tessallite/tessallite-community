"""End-to-end multi-named-role row-security guard (Bug-8017 / F-007-03).

The RLS predicate compiler correctly ORs named-role grants across a principal's
roles, but that branch was UNREACHABLE for a real authenticated user: the
production principal was built from ``CurrentUser.role`` (a single str), so a
user entitled to two named RLS roles (e.g. France + Germany) saw only ONE
role's rows. The existing compiler multi-role tests hand-BUILD a Principal with
two roles, so they passed while the real mint -> decode -> CurrentUser ->
Principal path was broken.

These tests drive the REAL token path:

    create_access_token(roles=[...])   (the actual mint)
      -> decode_access_token
      -> _build_user_from_payload -> CurrentUser
      -> Principal.from_current_user
      -> compile_row_security

and assert the emitted WHERE ORs BOTH countries' predicates. This is the guard
the hand-built-Principal test could not provide.

Test escape: the compiler OR-of-grants test constructed ``Principal(roles=
frozenset({"mgr_fr", "mgr_de"}))`` directly, never exercising the mint/decode
path that could only ever carry ONE role. Guard: this suite mints a real token
and drives the full principal-build path. Tier: T1 (producer/consumer security
contract) + T2 (Bug-8017 fixed-bug regression).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock

import pytest

from src.auth.local_backend import create_access_token
from shared.auth.jwt import decode_access_token
from shared.auth.middleware import CurrentUser, _build_user_from_payload
from shared.security.persona_resolver import caller_roles
from shared.security.predicate_compiler import (
    Principal,
    compile_row_security,
    has_active_rules,
)


# ---------------------------------------------------------------------------
# Fake DB supplying RowSecurityRule rows (no live DB required). Mirrors the
# query-router compiler-test fixture shape.
# ---------------------------------------------------------------------------


def _make_role_rule(name, path, expr, roles, rule_id=None):
    return types.SimpleNamespace(
        id=rule_id or uuid.uuid4(),
        rule_type="role_predicate",
        name=name,
        dimension_path=path,
        predicate_expression=expr,
        applies_to_roles=roles,
        attribute_source="jwt_role",
        attribute_claim_name=None,
        mapping_table_id=None,
        mapping_user_column=None,
        mapping_value_column=None,
        is_enabled=True,
    )


def _fake_db_with_rules(rules):
    class _Result:
        def __init__(self, items):
            self._items = list(items)

        def scalars(self):
            class _S:
                def __init__(_self, items):
                    _self._items = items

                def all(_self):
                    return _self._items

            return _S(self._items)

    db = AsyncMock()

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return _Result(rules)
        return _Result([])

    db.execute = _execute
    return db


def _principal_from_minted_token(*, role, roles):
    """Mint a REAL token, decode it, build CurrentUser, adapt to Principal —
    the exact production path (no hand-built Principal)."""
    token = create_access_token(
        sub="mgr@acme.com", tenant_id="acme", role=role, roles=roles,
    )
    payload = decode_access_token(token)
    user = _build_user_from_payload(payload)
    assert isinstance(user, CurrentUser)
    return user, Principal.from_current_user(user)


# ---------------------------------------------------------------------------
# THE guard: a real multi-role token ORs both grants end to end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_token_multi_role_ors_both_grants():
    """A France+Germany manager, authenticated through the REAL mint/decode
    path, sees BOTH countries' rows: the compiled predicate ORs the FR grant
    with the DE grant. Before the fix the single-role principal saw only one."""
    user, principal = _principal_from_minted_token(
        role="mgr_fr", roles=["mgr_fr", "mgr_de"],
    )
    # Producer/consumer alignment: the multi-role subject survived the token.
    assert user.roles == ["mgr_fr", "mgr_de"]
    assert principal.roles == frozenset({"mgr_fr", "mgr_de"})

    r_fr = _make_role_rule(
        "france", "region.region_code",
        "dimension_equals('region.region_code', 'FR')", ["mgr_fr"],
    )
    r_de = _make_role_rule(
        "germany", "region.region_code",
        "dimension_equals('region.region_code', 'DE')", ["mgr_de"],
    )
    db = _fake_db_with_rules([r_fr, r_de])
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    # UNION of the two named grants: OR, never AND (AND is the empty set).
    assert " OR " in out.sql_expression
    assert " AND " not in out.sql_expression
    assert "'FR'" in out.sql_expression and "'DE'" in out.sql_expression
    assert set(out.active_rule_ids) == {str(r_fr.id), str(r_de.id)}


@pytest.mark.asyncio
async def test_legacy_single_role_token_restricts_to_one_role():
    """A legacy token with ONLY a ``role`` claim (no ``roles``) must behave
    exactly as before the fix: restricted to that single role's predicate."""
    # No roles= param -> no ``roles`` claim minted -> legacy shape.
    token = create_access_token(sub="mgr@acme.com", tenant_id="acme", role="mgr_fr")
    payload = decode_access_token(token)
    assert "roles" not in payload  # legacy token carries no multi-role claim
    user = _build_user_from_payload(payload)
    assert user.roles == []  # falls back to {role}
    principal = Principal.from_current_user(user)
    assert principal.roles == frozenset({"mgr_fr"})

    r_fr = _make_role_rule(
        "france", "region.region_code",
        "dimension_equals('region.region_code', 'FR')", ["mgr_fr"],
    )
    r_de = _make_role_rule(
        "germany", "region.region_code",
        "dimension_equals('region.region_code', 'DE')", ["mgr_de"],
    )
    db = _fake_db_with_rules([r_fr, r_de])
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    # Only the FR grant applies; DE is not the caller's role.
    assert "'FR'" in out.sql_expression
    assert "'DE'" not in out.sql_expression
    assert set(out.active_rule_ids) == {str(r_fr.id)}


@pytest.mark.asyncio
async def test_unmatched_principal_denies_all_on_role_governed_model():
    """A real token whose role(s) match NO rule on a role-governed model must
    fail closed: deny-all (``0 = 1``), never unrestricted."""
    _user, principal = _principal_from_minted_token(
        role="viewer", roles=["viewer"],
    )
    r_fr = _make_role_rule(
        "france", "region.region_code",
        "dimension_equals('region.region_code', 'FR')", ["mgr_fr"],
    )
    db = _fake_db_with_rules([r_fr])
    out = await compile_row_security(uuid.uuid4(), principal, db)
    assert out is not None
    assert out.sql_expression == "0 = 1"
    assert has_active_rules(out) is True
    assert out.security_dimension_columns == ()


# ---------------------------------------------------------------------------
# Sibling consumer: persona_resolver.caller_roles now picks up the multi-role
# set (the roles attribute previously did not exist, so its branch was dead).
# ---------------------------------------------------------------------------


def test_caller_roles_returns_multi_role_set():
    """persona_resolver.caller_roles(user) unions ``role`` with the new
    ``roles`` collection — the previously-dead ``getattr(user, "roles", ...)``
    branch now returns the full set."""
    user = CurrentUser(
        user_id="a@x", tenant_id="acme", email="a@x",
        role="a", roles=["a", "b"],
    )
    assert caller_roles(user) == {"a", "b"}
