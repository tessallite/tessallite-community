"""Role-string constants, aliases, and helpers shared across services.

Keeps the grantable role vocabulary in one place so the local-user API,
the SSO group-mapping API, the RBAC checker, the JIT default resolver, and
the frontend role dropdowns can never drift apart (B1 deep-review finding
H-1; F-021-12 centralisation).

Role model:

* ``member`` / ``tenant_admin`` — base local-user roles.
  ``tenant_admin`` unlocks tenant administration; ``member`` relies on
  per-project access bindings (admin / modeler / viewer) for everything
  else.
* Project access roles are binding attributes: ``admin``, ``modeler``,
  ``viewer`` — see ``PROJECT_ROLE_HIERARCHY`` (highest -> lowest).
* ``MODEL_TECHNICAL_ROLE`` (``model_technical``) — an audience-role
  grant, not an RBAC tier. A user carrying it auto-resolves to each
  model's seeded Technical persona (the hidden-columns modeller view,
  see ``shared.security.persona_resolver``). It confers no project
  permissions; bindings still decide what the user may read or edit.
* Legacy/input-only aliases such as ``analyst`` and ``member`` normalize
  to project ``viewer`` semantics when they appear in project-role checks.
  Unknown tokens normalize to least privilege (never elevated).
"""
from __future__ import annotations

# --- Named role constants (single source of truth) -------------------------
SYSTEM_ADMIN_ROLE = "system_admin"
TENANT_ADMIN_ROLE = "tenant_admin"
MEMBER_ROLE = "member"
MODEL_TECHNICAL_ROLE = "model_technical"

PROJECT_ADMIN_ROLE = "admin"
PROJECT_MODELER_ROLE = "modeler"
PROJECT_VIEWER_ROLE = "viewer"

# Roles assignable to local users via the user-management API/UI.
ALLOWED_LOCAL_USER_ROLES: tuple[str, ...] = (
    MEMBER_ROLE,
    TENANT_ADMIN_ROLE,
    MODEL_TECHNICAL_ROLE,
)

# Project access roles, ordered highest -> lowest privilege. This is the
# canonical RBAC hierarchy consumed by the model-service ``require_role``.
PROJECT_ROLE_HIERARCHY: tuple[str, ...] = (
    PROJECT_ADMIN_ROLE,
    PROJECT_MODELER_ROLE,
    PROJECT_VIEWER_ROLE,
)
PROJECT_ROLE_SET: frozenset[str] = frozenset(PROJECT_ROLE_HIERARCHY)

# Roles assignable through SSO IdP group-to-role mappings. Project roles map
# into project RBAC; model_technical maps into persona/audience access. Value
# is unchanged from before centralisation: {admin, modeler, viewer,
# model_technical}.
ALLOWED_GROUP_MAPPING_ROLES: frozenset[str] = frozenset(
    {*PROJECT_ROLE_SET, MODEL_TECHNICAL_ROLE}
)

# JIT defaults are stored as ``LocalUser.role`` for backwards compatibility,
# but they must normalize through the project-role vocabulary. ``analyst``
# remains an accepted legacy alias and is viewer-equivalent. Any value not in
# this map falls back to viewer (least privilege) via
# ``normalize_jit_default_role``.
JIT_DEFAULT_ROLE_ALIASES: dict[str, str] = {
    PROJECT_VIEWER_ROLE: PROJECT_VIEWER_ROLE,
    "analyst": PROJECT_VIEWER_ROLE,
    PROJECT_MODELER_ROLE: PROJECT_MODELER_ROLE,
    MEMBER_ROLE: PROJECT_VIEWER_ROLE,
    MODEL_TECHNICAL_ROLE: MODEL_TECHNICAL_ROLE,
}

# Maps any project-role token (including legacy/audience aliases) to a real
# project RBAC tier. Anything absent here normalizes to None, which
# ``project_role_level`` treats as least privilege (deny). This is the
# fail-safe: an unknown role is never elevated.
PROJECT_ROLE_ALIASES: dict[str, str] = {
    PROJECT_ADMIN_ROLE: PROJECT_ADMIN_ROLE,
    PROJECT_MODELER_ROLE: PROJECT_MODELER_ROLE,
    PROJECT_VIEWER_ROLE: PROJECT_VIEWER_ROLE,
    "analyst": PROJECT_VIEWER_ROLE,
    MEMBER_ROLE: PROJECT_VIEWER_ROLE,
    MODEL_TECHNICAL_ROLE: PROJECT_VIEWER_ROLE,
}


def normalize_project_role(role: str | None) -> str | None:
    """Resolve a project-role token to a canonical RBAC tier, or None.

    Unknown tokens return None so callers treat them as least privilege.
    """
    if role is None:
        return None
    return PROJECT_ROLE_ALIASES.get(role)


def project_role_level(role: str | None) -> int:
    """Privilege index for a project role; lower means more privileged.

    Unknown / unrecognised roles return ``len(PROJECT_ROLE_HIERARCHY)`` — one
    step below ``viewer`` — so they can never satisfy any ``require_role``
    minimum (fail-safe / least privilege).
    """
    normalized = normalize_project_role(role)
    if normalized is None:
        return len(PROJECT_ROLE_HIERARCHY)
    return PROJECT_ROLE_HIERARCHY.index(normalized)


def normalize_jit_default_role(role: str | None) -> str:
    """Resolve a configured JIT default-role value to a safe role.

    None or any unrecognised value falls back to ``viewer`` (least privilege).
    """
    if role is None:
        return PROJECT_VIEWER_ROLE
    return JIT_DEFAULT_ROLE_ALIASES.get(role, PROJECT_VIEWER_ROLE)
