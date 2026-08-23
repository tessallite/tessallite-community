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

F-007-11: the simulate-as path carries groups and claims so admins can
exercise ``idp_group`` / ``saml_claim`` / ``oidc_scope`` row-security rules
through the gateway, matching what the model-service ``/simulate`` preview
already accepts.

Bug-7995 / F-024-01: embed sessions ARE now RLS-rule-filtered. An embed token
carries an admin-authored row-security subject (role/groups/claims) surfaced on
``CurrentEmbedUser`` under the same attribute names, so the non-simulate path's
``Principal.from_current_user(current_user)`` builds an embedded principal
identically to an interactive one. A bare embed token (no subject) yields an
empty role set, so a role-governed model fails closed (deny-all) for it. Persona
default filters remain a separate, additional policy system.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status

from shared.auth.middleware import CurrentUser
from shared.security import Principal
# Bug-8301: reuse the resolver's OWN privileged-role set rather than duplicating
# the literal, so the persona tier chosen for a simulated identity can never
# drift from what ``resolve_effective_persona`` treats as privileged.
from shared.security.persona_resolver import PRIVILEGED_ROLES as _PERSONA_PRIVILEGED_ROLES

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

    # Bug-7995 / F-024-01 hardening: an embed session can now carry any
    # admin-authored RLS role (incl. ``tenant_admin``). simulate-as is a
    # human-admin debugging surface only — an embed token must NEVER be able to
    # impersonate an arbitrary principal regardless of the RLS role it carries.
    # Reject on ``is_embed`` before the role check so a token minted with an
    # admin-shaped RLS role cannot reach the simulate path.
    if getattr(current_user, "is_embed", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Embed sessions cannot use simulate-as",
        )

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


def simulate_headers_present(
    simulate_principal: str | None,
    simulate_roles: str | None = None,
    simulate_groups: str | None = None,
    simulate_claims: str | None = None,
) -> bool:
    """True when ANY simulate-as header carries a value (simulation active).

    Mirrors the trigger condition in :func:`resolve_principal` so callers can
    decide whether to thread the simulated identity into persona resolution
    WITHOUT re-inferring it from the built principal (a real non-simulated user
    also carries a populated ``roles`` principal, so the principal alone cannot
    distinguish the two cases — Bug-8301).
    """
    return any(
        (v or "").strip()
        for v in (simulate_principal, simulate_roles, simulate_groups, simulate_claims)
    )


def persona_current_user_for_principal(
    current_user: CurrentUser,
    principal: Principal,
    *,
    simulated: bool,
) -> CurrentUser:
    """Return the ``CurrentUser`` that persona resolution must run against.

    Bug-8301: admin ``simulate-as`` applies the SIMULATED principal for row
    security (``resolve_principal``) but historically resolved the effective
    persona with the REAL admin ``current_user`` — so persona default-filters
    and persona-CLS were NOT faithfully simulated (the admin, being privileged,
    resolved to "no persona / any persona"). This threads the simulated identity
    into persona resolution so the persona audience/assignment matrix evaluates
    against the SIMULATED user's stated roles, exactly like the interactive path.

    SECURITY (never escalates): the returned user carries ONLY the simulated
    roles, so ``resolve_effective_persona`` gates persona entitlement against
    those stated roles — a simulated non-privileged user can NOT reach a
    privileged / visibility-widening persona unless its OWN stated roles grant
    it (identical to that user logging in directly). The admin's own privileged
    role is deliberately dropped: simulating ``roles=viewer`` must yield viewer's
    persona entitlement, not admin's.

    When ``simulated`` is False, the real caller is returned UNCHANGED so its
    real ``role`` / ``roles`` / ``persona_id`` (embed lock) all apply — a real
    non-simulated user cannot be inferred from ``principal`` alone because its
    principal also carries a populated role set. The caller passes the authority
    (``simulate_headers_present``), never a heuristic on the principal.

    The ``persona_resolver`` contract is untouched — this only constructs the
    already-resolved simulated identity the resolver already accepts.
    """
    # No simulation in effect -> resolve persona as the real caller.
    if not simulated:
        return current_user

    sim_roles = list(principal.roles)
    # The single RBAC tier drives ``is_privileged_by_role``. Grant a privileged
    # tier ONLY when the simulated roles genuinely include one — otherwise the
    # simulated user is a plain viewer for persona-resolution purposes. Never
    # inherit the admin's own ``role``.
    privileged = set(sim_roles) & _PERSONA_PRIVILEGED_ROLES
    sim_role = next(iter(privileged)) if privileged else "viewer"
    return CurrentUser(
        user_id=principal.user_identity,
        tenant_id=getattr(current_user, "tenant_id", ""),
        email=principal.user_identity,
        role=sim_role,
        roles=sim_roles,
        groups=list(principal.groups),
        claims=dict(principal.claims),
    )
