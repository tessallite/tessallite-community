"""Build the auth chain from the ``AUTH_BACKENDS`` setting.

Call ``get_auth_chain()`` at startup to instantiate the configured
backends in order.  The login endpoint passes credentials to the chain
and the first backend to recognise them wins.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from shared.auth.backend import AuthBackend, AuthChain
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)


_REDIRECT_BACKENDS = frozenset({"saml", "oidc"})


def _build_backends() -> list[AuthBackend]:
    settings = get_settings()
    names = [n.strip().lower() for n in settings.AUTH_BACKENDS.split(",") if n.strip()]
    backends: list[AuthBackend] = []
    for name in names:
        if name == "local":
            from src.auth.local_auth_backend import LocalAuthBackend
            backends.append(LocalAuthBackend())
        elif name == "ldap":
            from src.auth.ldap_backend import LdapAuthBackend
            backends.append(LdapAuthBackend())
        elif name == "gcp_iam":
            from src.auth.gcp_iam_backend import GcpIamAuthBackend
            backends.append(GcpIamAuthBackend())
        elif name in _REDIRECT_BACKENDS:
            logger.info("Redirect-based backend %r registered (handled by SSO endpoints)", name)
        else:
            logger.warning("Unknown auth backend %r — skipping", name)
    if not backends:
        logger.warning("No auth backends configured; falling back to local")
        from src.auth.local_auth_backend import LocalAuthBackend
        backends.append(LocalAuthBackend())
    return backends


@lru_cache
def get_auth_chain() -> AuthChain:
    backends = _build_backends()
    logger.info("Auth chain: %s", [b.name for b in backends])
    return AuthChain(backends)
