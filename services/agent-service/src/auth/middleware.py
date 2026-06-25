"""Backwards-compatible re-export of the shared auth middleware.

Mirrors the model-service / query-router / scheduler / optimizer pattern
so the agent-service can import the same dependencies.
"""
from shared.auth.middleware import (
    CurrentEmbedUser,
    CurrentUser,
    enforce_model_scope,
    forbid_embed_user,
    get_current_user,
    require_capability,
    require_system_admin,
    require_tenant_admin,
)

__all__ = [
    "CurrentEmbedUser",
    "CurrentUser",
    "enforce_model_scope",
    "forbid_embed_user",
    "get_current_user",
    "require_capability",
    "require_system_admin",
    "require_tenant_admin",
]
