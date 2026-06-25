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

    @property
    def backend_names(self) -> list[str]:
        return [b.name for b in self._backends]
