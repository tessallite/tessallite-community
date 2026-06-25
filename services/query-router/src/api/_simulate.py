"""Admin-only ``simulate-as`` principal for query-router endpoints.

Four headers, all admin-gated:

    X-Tessallite-Simulate-Principal  — the user identity (email / sub)
    X-Tessallite-Simulate-Roles      — comma-separated role list
    X-Tessallite-Simulate-Groups     — comma-separated IdP group list
    X-Tessallite-Simulate-Claims     — semicolon-separated key=value claims

Non-admin callers that send any header get 403. Both
``tenant_admin`` and ``system_admin`` callers may simulate any
principal — users can belong to multiple tenants. Absent headers
means "run as the caller".

F-007-11: the simulate-as path now carries groups and claims so admins
can exercise ``idp_group`` / ``saml_claim`` / ``oidc_scope`` row-security
rules through the gateway, matching what the model-service ``/simulate``
preview already accepts. Embed sessions are intentionally NOT extended —
they are persona-filtered (default filters), not RLS-rule-filtered (see
``help/modelling/configure-row-security.md``).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status

from shared.auth.middleware import CurrentUser
from shared.security import Principal

logger = logging.getLogger(__name__)

_ADMIN_ROLES = frozenset({"system_admin", "tenant_admin"})


def _parse_claims(raw: str | None) -> dict[str, Any]:
    """Parse the simulate-claims header (``key=value;key2=value2``).

    A value containing spaces (an OAuth scope string) is preserved
    verbatim — the predicate compiler splits scope strings itself.
    """
    out: dict[str, Any] = {}
    if not raw:
        return out
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        eq = part.find("=")
        if eq <= 0:
            continue
        key = part[:eq].strip()
        value = part[eq + 1 :].strip()
        if key:
            out[key] = value
    return out


def resolve_principal(
    current_user: CurrentUser,
    simulate_principal: str | None,
    simulate_roles: str | None,
    simulate_groups: str | None = None,
    simulate_claims: str | None = None,
) -> Principal:
    """Build the Principal to apply row security against.

    If the caller supplies any simulate header, they must hold an admin
    role — otherwise 403. Admins who omit the headers act as themselves.
    """
    sim_identity = (simulate_principal or "").strip()
    sim_roles_raw = (simulate_roles or "").strip()
    sim_groups_raw = (simulate_groups or "").strip()
    sim_claims_raw = (simulate_claims or "").strip()

    if not (sim_identity or sim_roles_raw or sim_groups_raw or sim_claims_raw):
        return Principal.from_current_user(current_user)

    if current_user.role not in _ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="simulate-as requires an admin role",
        )

    if not sim_identity:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "X-Tessallite-Simulate-Principal is required when any other "
                "X-Tessallite-Simulate-* header is set"
            ),
        )

    roles = frozenset(
        r.strip() for r in sim_roles_raw.split(",") if r.strip()
    )
    groups = frozenset(
        g.strip() for g in sim_groups_raw.split(",") if g.strip()
    )
    claims = _parse_claims(sim_claims_raw)

    logger.info(
        "[SIMULATE] user=%s tenant=%s simulated=%s roles=%s groups=%s claims=%s",
        current_user.email,
        current_user.tenant_id,
        sim_identity,
        ",".join(sorted(roles)) or "(none)",
        ",".join(sorted(groups)) or "(none)",
        ",".join(sorted(claims.keys())) or "(none)",
    )

    return Principal(
        user_identity=sim_identity,
        roles=roles,
        groups=groups,
        claims=claims,
    )
