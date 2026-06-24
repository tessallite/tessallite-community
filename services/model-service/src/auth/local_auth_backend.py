"""Local email/password auth backend using the ``local_users`` table.

Wraps the existing ``authenticate_user`` function behind the
``AuthBackend`` protocol so it can participate in the auth chain
alongside LDAP, GCP IAM, or other SSO backends.
"""
from __future__ import annotations

from typing import Any

from shared.auth.backend import AuthBackend, UserIdentity
from shared.db.session import get_tenant_db
from src.auth.local_backend import authenticate_user


class LocalAuthBackend:
    """Auth backend backed by the per-tenant ``local_users`` table."""

    name: str = "local"

    async def authenticate(
        self, *, tenant_id: str, email: str, password: str, **kwargs: Any
    ) -> UserIdentity | None:
        async for db in get_tenant_db(tenant_id):
            user = await authenticate_user(db, email, password)
            if user is None:
                return None
            return UserIdentity(
                email=user.email,
                display_name=user.username or user.email,
                groups=[],
                source_backend=self.name,
                raw_claims={"role": user.role, "user_id": str(user.id)},
            )
        return None


assert isinstance(LocalAuthBackend(), AuthBackend)
