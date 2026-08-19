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

# Built-in read-only *consumer* role (Bug-8101 / F-104-01, decision D2). A
# ``model_viewer`` binding governs AUTHORING capability only: it can open a
# model read-only, browse metadata, and run queries/pivots within its scope,
# but SAVE and DEPLOY are rejected. It sits at VIEWER privilege level in the
# RBAC hierarchy (below ``modeler``), so every existing
# ``require_role("modeler")`` gate already denies it 403 with no per-endpoint
# change — it REFINES ``viewer`` rather than adding a second overlapping tier.
# The distinction from the legacy ``viewer`` is intent/surface: a
# ``model_viewer`` principal is routed into the read-only builder surface
# (see the frontend ``caller_can_author`` gate) and participates in the
# Modeller-supersession invariant. Data-row visibility (RLS/CLS/persona) is
# unchanged and enforced downstream at the gateway regardless of this role.
PROJECT_MODEL_VIEWER_ROLE = "model_viewer"

# Roles assignable to local users via the user-management API/UI.
ALLOWED_LOCAL_USER_ROLES: tuple[str, ...] = (
    MEMBER_ROLE,
    TENANT_ADMIN_ROLE,
    MODEL_TECHNICAL_ROLE,
)

# Project access roles, ordered highest -> lowest privilege. This is the
# canonical RBAC hierarchy consumed by the model-service ``require_role``.
# ``model_viewer`` is intentionally NOT a distinct hierarchy rank — it aliases
# to ``viewer`` privilege via ``PROJECT_ROLE_ALIASES`` so the two read-only
# roles share one level (no overlapping tiers). It IS an assignable token, so
# it is added to ``PROJECT_ROLE_SET`` (grantable) separately below.
PROJECT_ROLE_HIERARCHY: tuple[str, ...] = (
    PROJECT_ADMIN_ROLE,
    PROJECT_MODELER_ROLE,
    PROJECT_VIEWER_ROLE,
)
# Grantable project-access role tokens. Includes ``model_viewer`` (a
# viewer-level read-only consumer role) in addition to the three hierarchy
# ranks, so the access API/UI may assign it while privilege ordering still
# resolves it to viewer level.
PROJECT_ROLE_SET: frozenset[str] = frozenset(
    {*PROJECT_ROLE_HIERARCHY, PROJECT_MODEL_VIEWER_ROLE}
)

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
    PROJECT_MODEL_VIEWER_ROLE: PROJECT_MODEL_VIEWER_ROLE,
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
    # A model_viewer binding is read-only at viewer privilege: it satisfies
    # require_role("viewer") (read + query within scope) and is rejected by
    # require_role("modeler")/("admin") (SAVE/DEPLOY/authoring) exactly like
    # viewer. It never elevates.
    PROJECT_MODEL_VIEWER_ROLE: PROJECT_VIEWER_ROLE,
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


def is_model_viewer_role(role: str | None) -> bool:
    """True iff ``role`` is the built-in read-only Model-viewer consumer role."""
    return role == PROJECT_MODEL_VIEWER_ROLE


def is_modeller_role(role: str | None) -> bool:
    """True iff ``role`` is a project ``modeler`` binding (authoring tier).

    Uses the canonical alias resolution so only a genuine modeler token counts
    (admin is a separate, higher tier and is handled independently — an admin
    binding is not part of the Modeller/Model-viewer mutual-exclusivity rule,
    which is specifically about the modeler authoring tier vs the read-only
    consumer role).
    """
    return role == PROJECT_MODELER_ROLE


def scope_covers(outer_model_id, inner_model_id) -> bool:
    """Whether a same-project binding scoped to ``outer_model_id`` COVERS
    (is a superset of, ⊇) the scope ``inner_model_id``.

    Used for the Modeller-supersedes-Model-viewer invariant, which fires only
    when the Modeller scope covers the Model-viewer scope — NOT on mere overlap.
    Coverage is asymmetric:

    - ``outer_model_id is None`` is the project-wide ([ALL] models) scope and
      covers every scope in the project (project-wide, or any concrete model).
    - a concrete ``outer_model_id`` covers only the SAME concrete model; it does
      NOT cover the project-wide scope (a model-scoped Modeller must not
      supersede a project-wide Model-viewer — that would strip the viewer's read
      access on other models where the user is not a Modeller, spec:44-46).

    Callers must first confirm both bindings are for the same project.
    """
    if outer_model_id is None:
        return True
    return inner_model_id is not None and str(outer_model_id) == str(inner_model_id)


def normalize_jit_default_role(role: str | None) -> str:
    """Resolve a configured JIT default-role value to a safe role.

    None or any unrecognised value falls back to ``viewer`` (least privilege).
    """
    if role is None:
        return PROJECT_VIEWER_ROLE
    return JIT_DEFAULT_ROLE_ALIASES.get(role, PROJECT_VIEWER_ROLE)
