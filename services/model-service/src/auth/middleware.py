"""Backwards-compatible re-export of the shared auth middleware.

The canonical implementation now lives in `shared/auth/middleware.py` so
query-router, scheduler, optimizer, and gateway can import the same
dependencies. Existing model-service imports continue to work via this
re-export.
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
