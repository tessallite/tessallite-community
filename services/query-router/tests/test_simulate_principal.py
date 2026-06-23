"""Phase 5.1 P5-1E2 — simulate-as headers on query-router routes.

Guards the admin-gated simulate-principal helper used by ``/execute``,
``/explain``, and the drill-through endpoint. Non-admin callers that
try to impersonate get 403; admins that omit the headers act as
themselves.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from shared.auth.middleware import CurrentUser
from src.api._simulate import resolve_principal


def _user(role: str | None) -> CurrentUser:
    return CurrentUser(
        user_id="admin@acme.com",
        tenant_id="tenant-1",
        email="admin@acme.com",
        role=role,
    )


def test_no_headers_returns_caller_principal():
    caller = _user("tenant_admin")
    p = resolve_principal(caller, None, None)
    assert p.user_identity == "admin@acme.com"
    assert "tenant_admin" in p.roles


def test_admin_can_simulate():
    caller = _user("tenant_admin")
    p = resolve_principal(
        caller,
        "alice@acme.com",
        "region_manager_north,viewer",
    )
    assert p.user_identity == "alice@acme.com"
    assert p.roles == frozenset({"region_manager_north", "viewer"})


def test_system_admin_can_simulate():
    caller = _user("system_admin")
    p = resolve_principal(caller, "bob@acme.com", "viewer")
    assert p.user_identity == "bob@acme.com"
    assert p.roles == frozenset({"viewer"})


def test_non_admin_simulate_is_403():
    caller = _user("viewer")
    with pytest.raises(HTTPException) as exc:
        resolve_principal(caller, "alice@acme.com", "region_manager_north")
    assert exc.value.status_code == 403


def test_admin_role_absent_is_403():
    caller = _user(None)
    with pytest.raises(HTTPException) as exc:
        resolve_principal(caller, "alice@acme.com", None)
    assert exc.value.status_code == 403


def test_roles_header_without_identity_is_400():
    caller = _user("tenant_admin")
    with pytest.raises(HTTPException) as exc:
        resolve_principal(caller, None, "viewer,region_manager_north")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# F-007-11 — simulate-as now carries groups + claims so an admin can
# exercise idp_group / saml_claim / oidc_scope rules through the gateway.
# ---------------------------------------------------------------------------


def test_admin_can_simulate_groups():
    caller = _user("tenant_admin")
    p = resolve_principal(
        caller, "alice@acme.com", None,
        simulate_groups="finance,leadership",
    )
    assert p.user_identity == "alice@acme.com"
    assert p.groups == frozenset({"finance", "leadership"})


def test_admin_can_simulate_claims():
    caller = _user("tenant_admin")
    p = resolve_principal(
        caller, "alice@acme.com", None,
        simulate_claims="department=finance;scope=openid reports:read",
    )
    assert p.claims == {
        "department": "finance",
        "scope": "openid reports:read",
    }


def test_groups_header_without_identity_is_400():
    # Fail closed: any simulate-* header without a principal is rejected so
    # a half-specified impersonation never silently runs as the caller.
    caller = _user("tenant_admin")
    with pytest.raises(HTTPException) as exc:
        resolve_principal(caller, None, None, simulate_groups="finance")
    assert exc.value.status_code == 400


def test_claims_header_without_identity_is_400():
    caller = _user("tenant_admin")
    with pytest.raises(HTTPException) as exc:
        resolve_principal(caller, None, None, simulate_claims="dept=finance")
    assert exc.value.status_code == 400


def test_non_admin_simulate_groups_is_403():
    # A non-admin supplying only a groups header must still be denied —
    # the admin gate covers every simulate-* header, not just roles.
    caller = _user("viewer")
    with pytest.raises(HTTPException) as exc:
        resolve_principal(
            caller, "alice@acme.com", None, simulate_groups="finance",
        )
    assert exc.value.status_code == 403
