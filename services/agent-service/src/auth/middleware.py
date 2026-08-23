"""Backwards-compatible re-export of the shared auth middleware.

Mirrors the model-service / query-router / scheduler / optimizer pattern
so the agent-service can import the same dependencies.
"""
from shared.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    enforce_model_scope,
    forbid_embed_user,
    get_current_user,
    is_canonical_human_system_admin,
    is_human_tenant_admin,
    is_human_tenant_admin_or_system_admin,
    require_capability,
    require_capability_or_service_scope,
    require_service_scope_or_non_embed,
    require_service_scope_or_system_admin,
    require_service_scope_or_tenant_admin,
    require_system_admin,
    require_tenant_admin,
)

__all__ = [
    "CurrentEmbedUser",
    "CurrentServiceUser",
    "CurrentUser",
    "enforce_model_scope",
    "forbid_embed_user",
    "get_current_user",
    "is_canonical_human_system_admin",
    "is_human_tenant_admin",
    "is_human_tenant_admin_or_system_admin",
    "require_capability",
    "require_capability_or_service_scope",
    "require_service_scope_or_non_embed",
    "require_service_scope_or_system_admin",
    "require_service_scope_or_tenant_admin",
    "require_system_admin",
    "require_tenant_admin",
]
