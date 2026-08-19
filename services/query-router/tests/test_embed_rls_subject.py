"""Row-security enforcement for EMBEDDED sessions (Bug-7995 / F-024-01).

Before this lane, an embed token carried no RLS role/group/claim, so
attribute-based (idp_group / saml_claim / oidc_scope) and role-based (jwt_role)
row-security rules could not fire for an embedded subject. These tests prove the
whole producer -> consumer chain now enforces RLS for embedded sessions:

    embed token claim  ->  CurrentEmbedUser (middleware surfaces role/groups/claims)
    ->  Principal.from_current_user  ->  compile_row_security  ->  route_query
        (per-scan WHERE injection over the security column in the inner SELECT).

The routing tests use the row-security probe pattern: the security dimension
column appears in the inner SELECT and the query GROUPs BY it, so the injected
predicate lands on the exact scanned column. An embedded principal is built
identically to an interactive one; a subject-less embed token FAILS CLOSED
(deny-all ``0 = 1``) on a role-governed model.

Test escape: prior embed tests only asserted project/model/persona/capability
scope, never that a role/group/claim RLS rule fires (or fails closed) for an
embed principal. Guard: this suite. Tier: T1 (producer/consumer security
contract) + T2 (F-024-01 fixed-bug regression).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.auth.middleware import CurrentEmbedUser, CurrentUser, _build_user_from_payload
from src.routing.router import route_query
from src.security import Principal

# Reuse the routing-test fixtures (same directory).
from test_row_security_routing import _db_returning, _role_rule
from conftest import make_aggregate, make_agg_col, make_dimension, make_measure
from test_query_flow import _bind

pytestmark = pytest.mark.integration

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"

_SQL = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"


def _bound_region_query():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    bq = _bind(_SQL, [m], [d])
    return bq, agg


def _embed_user(*, rls_role=None, groups=None, claims=None):
    """Construct the embed user exactly as the middleware would from a token."""
    return CurrentEmbedUser(
        user_id="viewer@customer.com",
        tenant_id="acme",
        email="viewer@customer.com",
        rls_role=rls_role,
        groups=groups,
        claims=claims,
    )


# ---------------------------------------------------------------------------
# Producer: middleware surfaces the embed RLS subject under the SAME attribute
# names the principal adapter reads.
# ---------------------------------------------------------------------------


class TestEmbedTokenSurfacesSubject:
    def test_embed_payload_carries_role_groups_claims_onto_principal(self):
        payload = {
            "sub": "viewer@customer.com",
            "tenant_id": "acme",
            "aud": "embed",
            "role": "finance",
            "groups": ["finance", "emea"],
            "claims": {"department": "sales"},
        }
        user = _build_user_from_payload(payload)
        assert isinstance(user, CurrentEmbedUser)
        # Producer/consumer alignment: same attribute names the adapter reads.
        assert user.role == "finance"
        assert user.groups == ["finance", "emea"]
        assert user.claims == {"department": "sales"}

        principal = Principal.from_current_user(user)
        assert principal.roles == frozenset({"finance"})
        assert principal.groups == frozenset({"finance", "emea"})
        assert principal.claims == {"department": "sales"}

    def test_bare_embed_token_has_no_rls_role(self):
        """A subject-less embed token must yield an EMPTY role set so a
        role-governed model fails closed — the ``embed`` sentinel is not an
        IdP role and must never match a rule literally targeting ``embed``."""
        payload = {"sub": "v", "tenant_id": "acme", "aud": "embed"}
        user = _build_user_from_payload(payload)
        assert isinstance(user, CurrentEmbedUser)
        assert user.role == "embed"  # sentinel, for is_embed RBAC guards
        principal = Principal.from_current_user(user)
        assert principal.roles == frozenset()
        assert principal.groups == frozenset()
        assert principal.claims == {}

    def test_rule_targeting_literal_embed_does_not_match_bare_embed(self):
        """Defence: even a rule that lists the literal role ``embed`` must not
        match a subject-less embed session (the sentinel is dropped)."""
        principal = Principal.from_current_user(_embed_user())
        assert "embed" not in principal.roles


# ---------------------------------------------------------------------------
# Consumer: an embedded principal is RESTRICTED by its subject, end to end,
# for every RLS attribute source — proven through the router probe pattern.
# ---------------------------------------------------------------------------


async def _route_for_embed_principal(rule, *, rls_role=None, groups=None, claims=None):
    bq, agg = _bound_region_query()
    principal = Principal.from_current_user(
        _embed_user(rls_role=rls_role, groups=groups, claims=claims)
    )
    db = _db_returning([rule])
    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        return await route_query(bq, db, principal=principal)


class TestEmbeddedSessionRestrictedBySubject:
    async def test_jwt_role_rule_restricts_embedded_session(self):
        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["region_manager_north"],
            attribute_source="jwt_role",
        )
        decision = await _route_for_embed_principal(
            rule, rls_role="region_manager_north"
        )
        # The security column is in the inner SELECT; the predicate lands on it.
        assert "\"region_code\" = 'NORTH'" in decision.rewritten_query

    async def test_idp_group_rule_restricts_embedded_session(self):
        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["finance"],
            attribute_source="idp_group",
        )
        decision = await _route_for_embed_principal(rule, groups=["finance"])
        assert "\"region_code\" = 'NORTH'" in decision.rewritten_query

    async def test_saml_claim_rule_restricts_embedded_session(self):
        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["sales"],
            attribute_source="saml_claim",
            attribute_claim_name="department",
        )
        decision = await _route_for_embed_principal(
            rule, claims={"department": "sales"}
        )
        assert "\"region_code\" = 'NORTH'" in decision.rewritten_query

    async def test_oidc_scope_rule_restricts_embedded_session(self):
        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["reports:read"],
            attribute_source="oidc_scope",
            attribute_claim_name="scope",
        )
        # OAuth scopes are conventionally a space-delimited string.
        decision = await _route_for_embed_principal(
            rule, claims={"scope": "openid profile reports:read"}
        )
        assert "\"region_code\" = 'NORTH'" in decision.rewritten_query


# ---------------------------------------------------------------------------
# Fail-closed: a subject-less (or wrong-subject) embed session on a role-governed
# model is DENIED every row, never served unrestricted.
# ---------------------------------------------------------------------------


class TestEmbeddedSessionFailsClosed:
    @pytest.mark.parametrize(
        "attribute_source,claim_name,rule_roles",
        [
            ("jwt_role", None, ["region_manager_north"]),
            ("idp_group", None, ["finance"]),
            ("saml_claim", "department", ["sales"]),
            ("oidc_scope", "scope", ["reports:read"]),
        ],
    )
    async def test_subject_absent_denies_all_rows(
        self, attribute_source, claim_name, rule_roles
    ):
        """The model is role-governed but the embed token carries NO RLS subject
        for that source. The session must be DENIED every row (``0 = 1``) and
        must NOT be served the unrestricted aggregate."""
        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            rule_roles,
            attribute_source=attribute_source,
            attribute_claim_name=claim_name,
        )
        decision = await _route_for_embed_principal(rule)  # bare embed token
        assert decision.route_type != "aggregate"
        assert decision.aggregate_id is None
        assert "0 = 1" in decision.rewritten_query

    async def test_wrong_subject_denies_all_rows(self):
        """An embed subject that matches NO rule (wrong group) fails closed."""
        rule = _role_rule(
            "region.region_code",
            "dimension_equals('region.region_code', 'NORTH')",
            ["finance"],
            attribute_source="idp_group",
        )
        decision = await _route_for_embed_principal(rule, groups=["marketing"])
        assert decision.route_type != "aggregate"
        assert "0 = 1" in decision.rewritten_query


# ---------------------------------------------------------------------------
# Spoofing: an embed token cannot use the simulate-as admin surface even if it
# carries an admin-shaped RLS role.
# ---------------------------------------------------------------------------


class TestEmbedCannotSimulate:
    def test_embed_with_admin_rls_role_cannot_simulate(self):
        from fastapi import HTTPException
        from src.api._simulate import resolve_principal

        # An embed token minted (by mistake or malice) with an admin-shaped RLS
        # role must still be barred from the simulate-as impersonation surface.
        user = _embed_user(rls_role="tenant_admin")
        with pytest.raises(HTTPException) as exc_info:
            resolve_principal(
                user,
                simulate_principal="victim@x",
                simulate_roles="region_manager_north",
            )
        assert exc_info.value.status_code == 403
        assert "embed" in exc_info.value.detail.lower()

    def test_non_simulate_embed_builds_its_own_principal(self):
        """No simulate headers: the embed principal is built from its own token
        subject, not impersonated."""
        from src.api._simulate import resolve_principal

        user = _embed_user(rls_role="finance", groups=["emea"])
        principal = resolve_principal(user, None, None, None, None)
        assert principal.roles == frozenset({"finance"})
        assert principal.groups == frozenset({"emea"})
