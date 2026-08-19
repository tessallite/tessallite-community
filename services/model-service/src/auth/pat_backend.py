"""Personal Access Token auth backend (Bug-7314).

Participates in the model-service auth chain. When the presented ``password``
is a PAT (``tesspat_`` prefix), it is validated against the tenant's
``personal_access_tokens`` table and resolved to the owning ``local_users`` row.
A non-PAT password short-circuits to ``None`` cheaply (no DB hit, no bcrypt),
so this backend can sit first in the chain without adding cost to normal
password logins.

The returned ``UserIdentity`` carries ``role`` read LIVE from the resolved user,
so the login endpoint mints a JWT scoped to the user's current tenant + role —
the PAT itself never stores role/tenant, so revocation and role changes take
effect immediately.
"""
from __future__ import annotations

from typing import Any

from shared.auth.backend import AuthBackend, UserIdentity
from shared.db.session import get_tenant_db

from src.auth.pat import _public_id, is_pat_scheme, touch_last_used, validate_pat


class PatAuthBackend:
    """Auth backend that accepts a Personal Access Token as the password."""

    name: str = "pat"

    async def authenticate(
        self, *, tenant_id: str, email: str, password: str, **kwargs: Any
    ) -> UserIdentity | None:
        # Cheap non-DB short-circuit: only PAT-scheme secrets are handled here.
        # A malformed/oversized PAT-scheme string still enters (so it is handled
        # terminally, never forwarded) — validate_pat rejects it internally
        # before any bcrypt, so a bad PAT costs at most one prefix lookup.
        if not is_pat_scheme(password):
            return None
        async for db in get_tenant_db(tenant_id):
            user = await validate_pat(db, token=password, email=email)
            if user is None:
                return None
            # Record usage ONLY after a successful validation, best-effort, on
            # this same dedicated session. A telemetry write failure is
            # swallowed inside touch_last_used and can never reject the login.
            await touch_last_used(
                db, token_prefix=_public_id(password), user_id=user.id
            )
            return UserIdentity(
                email=user.email,
                display_name=user.username or user.email,
                groups=[],
                source_backend=self.name,
                raw_claims={"role": user.role, "user_id": str(user.id)},
            )
        return None


assert isinstance(PatAuthBackend(), AuthBackend)
