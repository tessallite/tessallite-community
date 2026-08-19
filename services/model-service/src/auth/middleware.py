"""Backwards-compatible re-export of the shared auth middleware.

The canonical implementation now lives in `shared/auth/middleware.py` so
query-router, scheduler, optimizer, and gateway can import the same
dependencies. Existing model-service imports continue to work via this
re-export.
"""
from shared.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    bump_system_admin_token_version,
    enforce_model_scope,
    extract_token_optional,
    forbid_embed_user,
    forbid_service_user,
    get_current_user,
    get_system_admin_token_version,
    is_canonical_human_system_admin,
    is_human_tenant_admin,
    is_human_tenant_admin_or_system_admin,
    require_capability,
    require_capability_or_service_scope,
    require_human_user,
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
    "bump_system_admin_token_version",
    "enforce_model_scope",
    "extract_token_optional",
    "forbid_embed_user",
    "forbid_service_user",
    "get_current_user",
    "get_system_admin_token_version",
    "is_canonical_human_system_admin",
    "is_human_tenant_admin",
    "is_human_tenant_admin_or_system_admin",
    "require_capability",
    "require_capability_or_service_scope",
    "require_human_user",
    "require_service_scope_or_non_embed",
    "require_service_scope_or_system_admin",
    "require_service_scope_or_tenant_admin",
    "require_system_admin",
    "require_tenant_admin",
]
