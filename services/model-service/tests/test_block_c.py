"""Tests for Block C — Row Security Dynamic Attribute Mapping + Audit Trail."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.security.predicate_compiler import (
    Principal,
    _rule_applies_to_principal,
    compile_row_security,
)
from src.main import app
from src.auth.middleware import CurrentUser, get_current_user, require_tenant_admin
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rule(
    rule_type: str = "role_predicate",
    applies_to_roles: list[str] | None = None,
    predicate_expression: str = "dimension_equals('region.code', 'NORTH')",
    attribute_source: str = "jwt_role",
    attribute_claim_name: str | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name="test-rule",
        rule_type=rule_type,
        applies_to_roles=applies_to_roles or ["viewer"],
        predicate_expression=predicate_expression,
        dimension_path="region.code",
        is_enabled=True,
        attribute_source=attribute_source,
        attribute_claim_name=attribute_claim_name,
        mapping_table_id=None,
        mapping_user_column=None,
        mapping_value_column=None,
    )


# ---------------------------------------------------------------------------
# C.1 — Dynamic attribute matching unit tests
# ---------------------------------------------------------------------------

def test_jwt_role_matching_unchanged():
    """Existing jwt_role behavior is unaffected by attribute_source=jwt_role."""
    rule = _make_rule(applies_to_roles=["viewer"], attribute_source="jwt_role")
    principal = Principal(user_identity="u@example.com", roles=frozenset(["viewer"]))
    assert _rule_applies_to_principal(rule, principal) is True


def test_jwt_role_no_match():
    """Principal with wrong role does not match jwt_role rule."""
    rule = _make_rule(applies_to_roles=["admin"], attribute_source="jwt_role")
    principal = Principal(user_identity="u@example.com", roles=frozenset(["viewer"]))
    assert _rule_applies_to_principal(rule, principal) is False


def test_idp_group_matching():
    """attribute_source='idp_group' matches against principal.groups."""
    rule = _make_rule(applies_to_roles=["sales-team"], attribute_source="idp_group")
    principal = Principal(
        user_identity="u@example.com",
        roles=frozenset(["viewer"]),  # role doesn't match
        groups=frozenset(["sales-team", "marketing"]),  # group does match
    )
    assert _rule_applies_to_principal(rule, principal) is True


def test_idp_group_no_match():
    """Principal not in the IdP group does not match."""
    rule = _make_rule(applies_to_roles=["sales-team"], attribute_source="idp_group")
    principal = Principal(
        user_identity="u@example.com",
        roles=frozenset(["sales-team"]),  # role matches but source is idp_group
        groups=frozenset(["marketing"]),
    )
    assert _rule_applies_to_principal(rule, principal) is False


def test_saml_claim_matching():
    """attribute_source='saml_claim' reads from principal.claims[attribute_claim_name]."""
    rule = _make_rule(
        applies_to_roles=["north-region"],
        attribute_source="saml_claim",
        attribute_claim_name="http://schemas.example.com/claims/region",
    )
    principal = Principal(
        user_identity="u@example.com",
        roles=frozenset(),
        groups=frozenset(),
        claims={"http://schemas.example.com/claims/region": ["north-region", "us-east"]},
    )
    assert _rule_applies_to_principal(rule, principal) is True


def test_oidc_scope_matching():
    """attribute_source='oidc_scope' reads from principal.claims[attribute_claim_name]."""
    rule = _make_rule(
        applies_to_roles=["read:reports"],
        attribute_source="oidc_scope",
        attribute_claim_name="scope",
    )
    principal = Principal(
        user_identity="u@example.com",
        roles=frozenset(),
        groups=frozenset(),
        claims={"scope": "read:reports write:reports"},
    )
    # "read:reports write:reports" → wrapped as frozenset(["read:reports write:reports"])
    # The full scope string is one entry; to match "read:reports" it must be exact
    principal_with_list_scope = Principal(
        user_identity="u@example.com",
        roles=frozenset(),
        groups=frozenset(),
        claims={"scope": ["read:reports", "write:reports"]},
    )
    assert _rule_applies_to_principal(rule, principal_with_list_scope) is True


def test_saml_claim_missing_claim_name_no_match():
    """attribute_source='saml_claim' with no attribute_claim_name never matches."""
    rule = _make_rule(
        applies_to_roles=["admin"],
        attribute_source="saml_claim",
        attribute_claim_name=None,
    )
    principal = Principal(user_identity="u@example.com", claims={"x": ["admin"]})
    assert _rule_applies_to_principal(rule, principal) is False


def test_principal_from_current_user_carries_groups():
    """Principal.from_current_user populates groups from CurrentUser.groups."""
    user = CurrentUser(
        user_id="u@example.com", tenant_id="t1", email="u@example.com",
        role="viewer", groups=["sales-team", "marketing"],
    )
    p = Principal.from_current_user(user)
    assert p.groups == frozenset(["sales-team", "marketing"])
    assert p.roles == frozenset(["viewer"])


# ---------------------------------------------------------------------------
# C.2 — Audit trail: compile_row_security populates applied_rules
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compile_row_security_populates_applied_rules():
    """compiled.applied_rules contains rule_id, rule_name, predicate_sql for each match."""
    rule_id = uuid.uuid4()
    rule = types.SimpleNamespace(
        id=rule_id,
        name="north-filter",
        model_id=TEST_MODEL_ID,
        rule_type="role_predicate",
        applies_to_roles=["viewer"],
        predicate_expression="dimension_equals('region.code', 'NORTH')",
        dimension_path="region.code",
        is_enabled=True,
        attribute_source="jwt_role",
        attribute_claim_name=None,
        mapping_table_id=None,
        mapping_user_column=None,
        mapping_value_column=None,
    )

    db = AsyncMock()
    rules_result = MagicMock()
    rules_result.scalars.return_value.all.return_value = [rule]
    db.execute = AsyncMock(return_value=rules_result)

    principal = Principal(user_identity="u@example.com", roles=frozenset(["viewer"]))
    compiled = await compile_row_security(TEST_MODEL_ID, principal, db)

    assert compiled is not None
    assert len(compiled.applied_rules) == 1
    entry = compiled.applied_rules[0]
    assert entry["rule_id"] == str(rule_id)
    assert entry["rule_name"] == "north-filter"
    assert "NORTH" in entry["predicate_sql"]


# ---------------------------------------------------------------------------
# C.2 — Security audit API tests
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_admin():
    user = CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_tenant_admin] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_tenant_admin, None)


@pytest.fixture
async def admin_client(auth_admin):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_security_audit_list_empty(admin_client):
    """Returns 200 with items=[] and total=0 when no secured queries exist."""
    mock_db = make_mock_db()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    with patch("src.api.security_audit.get_tenant_db", async_gen_from(mock_db)):
        resp = await admin_client.get("/api/v1/admin/security-audit")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_security_audit_list_returns_entries(admin_client):
    """Returns query log entries that have security_rules_applied."""
    log_id = uuid.uuid4()
    mock_log = types.SimpleNamespace(
        id=log_id,
        model_id=TEST_MODEL_ID,
        user_identity="u@example.com",
        protocol="jdbc",
        route_type="source",
        security_rules_applied=[
            {"rule_id": str(uuid.uuid4()), "rule_name": "north-filter", "predicate_sql": '"code" = \'NORTH\''}
        ],
        created_at=NOW,
    )

    mock_db = make_mock_db()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [mock_log]
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    with patch("src.api.security_audit.get_tenant_db", async_gen_from(mock_db)):
        resp = await admin_client.get("/api/v1/admin/security-audit")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["user_identity"] == "u@example.com"
    assert len(item["security_rules_applied"]) == 1
    assert item["security_rules_applied"][0]["rule_name"] == "north-filter"
