"""Auth backend protocol and chain for pluggable authentication.

Each backend implements ``authenticate`` which takes credentials and returns
a ``UserIdentity`` on success or ``None`` on failure.  The ``AuthChain``
tries backends in order — the first to return a non-None identity wins.

Token validation is unchanged: every service uses the shared JWT decode
in ``shared/auth/jwt.py``.  Backends affect only the *issuance* path in
model-service's login endpoint.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserIdentity:
    """Canonical identity produced by any auth backend."""
    email: str
    display_name: str = ""
    groups: list[str] = field(default_factory=list)
    source_backend: str = ""
    raw_claims: dict[str, Any] = field(default_factory=dict)
    # F-021-04: whether the IdP actually RETURNED the configured groups claim.
    # ``groups=[]`` is ambiguous — it can mean "the IdP omitted group claims"
    # (indeterminate, must NOT revoke) or "the IdP authoritatively returned an
    # empty group set" (de-provisioning signal, MUST revoke SSO-derived grants).
    # Backends set this True only when the configured groups attribute/claim key
    # was present in the assertion/token (even if its value was empty). The JIT
    # reconciliation consumes it to revoke on a present-empty claim while
    # retaining grants when the claim is genuinely absent. Defaults to False so
    # any backend that does not populate it fails closed (no spurious revoke).
    groups_claim_present: bool = False


@dataclass(frozen=True)
class AuthOutcome:
    status: str
    identity: UserIdentity | None = None
    backend_name: str | None = None
    error: Exception | None = None

    @property
    def authenticated(self) -> bool:
        return self.status == "authenticated" and self.identity is not None

    @property
    def backend_error(self) -> bool:
        return self.status == "backend_error"


@runtime_checkable
class AuthBackend(Protocol):
    """Pluggable authentication backend."""

    name: str

    async def authenticate(
        self, *, tenant_id: str, email: str, password: str, **kwargs: Any
    ) -> UserIdentity | None:
        """Return a ``UserIdentity`` if the credentials are valid, else ``None``."""
        ...


class AuthChain:
    """Ordered list of backends — first success wins."""

    def __init__(self, backends: list[AuthBackend]) -> None:
        self._backends = list(backends)

    async def authenticate(
        self, *, tenant_id: str, email: str, password: str, **kwargs: Any
    ) -> UserIdentity | None:
        for backend in self._backends:
            try:
                identity = await backend.authenticate(
                    tenant_id=tenant_id, email=email, password=password, **kwargs
                )
            except Exception:
                logger.warning(
                    "Auth backend %r raised during authenticate for %s",
                    backend.name, email, exc_info=True,
                )
                continue
            if identity is not None:
                logger.info(
                    "User %s authenticated via %s", email, backend.name,
                )
                return identity
        return None

    async def authenticate_outcome(
        self, *, tenant_id: str, email: str, password: str, **kwargs: Any
    ) -> AuthOutcome:
        for backend in self._backends:
            try:
                identity = await backend.authenticate(
                    tenant_id=tenant_id, email=email, password=password, **kwargs
                )
            except Exception as exc:
                logger.warning(
                    "Auth backend %r raised during authenticate for %s",
                    backend.name, email, exc_info=True,
                )
                return AuthOutcome(
                    status="backend_error",
                    backend_name=backend.name,
                    error=exc,
                )
            if identity is not None:
                logger.info("User %s authenticated via %s", email, backend.name)
                return AuthOutcome(
                    status="authenticated",
                    identity=identity,
                    backend_name=backend.name,
                )
        return AuthOutcome(status="rejected")

    @property
    def backend_names(self) -> list[str]:
        return [b.name for b in self._backends]
