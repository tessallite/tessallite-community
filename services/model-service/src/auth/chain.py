"""Build the auth chain from the ``AUTH_BACKENDS`` setting.

Call ``get_auth_chain()`` at startup to instantiate the configured
backends in order.  The login endpoint passes credentials to the chain
and the first backend to recognise them wins.

Bug-7314 (external review finding 1): a Personal Access Token is a bearer
secret. If a PAT-shaped password is not a valid PAT, the chain must NOT fall
through to the other backends — a fall-through would submit the PAT verbatim to
the LDAP bind (leaking the secret to an external directory) or to the local
password verifier. ``PatTerminalAuthChain`` makes any ``tesspat_``-shaped
credential terminal: only the PAT backend may handle it, and its rejection is
final. This also reserves the ``tesspat_`` namespace so a local password that
happens to start with it can never authenticate as a non-PAT.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from shared.auth.backend import AuthBackend, AuthChain, AuthOutcome, UserIdentity
from shared.config.settings import get_settings

from src.auth.pat import is_pat_scheme

logger = logging.getLogger(__name__)


_REDIRECT_BACKENDS = frozenset({"saml", "oidc"})
_PAT_BACKEND_NAME = "pat"


class PatTerminalAuthChain(AuthChain):
    """Auth chain that makes PAT-scheme credentials terminal (Bug-7314).

    Routing is by SCHEME (``is_pat_scheme`` — anything starting with
    ``tesspat_``), NOT by format validity. A malformed or oversized
    ``tesspat_``-string is still routed terminally to the PAT backend (which
    rejects it), so a bearer secret — or anything shaped like one — is NEVER
    forwarded to LDAP/local (external review R2 finding 1). Success or failure is
    final. Non-scheme passwords behave exactly like the base chain.
    """

    def _pat_backend(self) -> AuthBackend | None:
        for backend in self._backends:
            if backend.name == _PAT_BACKEND_NAME:
                return backend
        return None

    async def authenticate(
        self, *, tenant_id: str, email: str, password: str, **kwargs
    ) -> UserIdentity | None:
        if is_pat_scheme(password):
            backend = self._pat_backend()
            if backend is None:
                return None
            try:
                return await backend.authenticate(
                    tenant_id=tenant_id, email=email, password=password, **kwargs
                )
            except Exception:
                logger.warning(
                    "PAT backend raised during authenticate for %s", email,
                    exc_info=True,
                )
                return None
        return await super().authenticate(
            tenant_id=tenant_id, email=email, password=password, **kwargs
        )

    async def authenticate_outcome(
        self, *, tenant_id: str, email: str, password: str, **kwargs
    ) -> AuthOutcome:
        if is_pat_scheme(password):
            backend = self._pat_backend()
            if backend is None:
                return AuthOutcome(status="rejected")
            try:
                identity = await backend.authenticate(
                    tenant_id=tenant_id, email=email, password=password, **kwargs
                )
            except Exception as exc:
                logger.warning(
                    "PAT backend raised during authenticate for %s", email,
                    exc_info=True,
                )
                return AuthOutcome(
                    status="backend_error",
                    backend_name=_PAT_BACKEND_NAME,
                    error=exc,
                )
            if identity is not None:
                return AuthOutcome(
                    status="authenticated",
                    identity=identity,
                    backend_name=_PAT_BACKEND_NAME,
                )
            # Terminal: a PAT-shaped credential that the PAT backend rejects is
            # final — do NOT fall through to LDAP/local with the bearer secret.
            return AuthOutcome(status="rejected")
        return await super().authenticate_outcome(
            tenant_id=tenant_id, email=email, password=password, **kwargs
        )


def _build_backends() -> list[AuthBackend]:
    settings = get_settings()
    names = [n.strip().lower() for n in settings.AUTH_BACKENDS.split(",") if n.strip()]
    backends: list[AuthBackend] = []
    # Bug-7314: the Personal Access Token backend is ALWAYS first, independent
    # of AUTH_BACKENDS. A PAT is a self-contained bearer secret that any user
    # (local, LDAP, or redirect-SSO) can mint to authenticate BI clients; SSO
    # users have no other JDBC/XMLA credential. A non-PAT password short-
    # circuits in the backend without a DB hit, so this adds no cost to normal
    # password logins.
    from src.auth.pat_backend import PatAuthBackend
    backends.append(PatAuthBackend())
    for name in names:
        if name == "local":
            from src.auth.local_auth_backend import LocalAuthBackend
            backends.append(LocalAuthBackend())
        elif name == "ldap":
            if not bool(getattr(settings, "LDAP_ENABLED", False)):
                logger.info("LDAP auth backend listed but LDAP_ENABLED is false; skipping")
                continue
            from src.auth.ldap_backend import LdapAuthBackend
            backends.append(LdapAuthBackend())
        elif name == "gcp_iam":
            from src.auth.gcp_iam_backend import GcpIamAuthBackend
            backends.append(GcpIamAuthBackend())
        elif name in _REDIRECT_BACKENDS:
            logger.info("Redirect-based backend %r registered (handled by SSO endpoints)", name)
        else:
            logger.warning("Unknown auth backend %r — skipping", name)
    # The PAT backend is always present but only handles PAT-shaped secrets, so
    # it does not count as a configured *credential* backend. If no credential
    # backend was configured, fall back to local so password login still works.
    if not any(b.name != _PAT_BACKEND_NAME for b in backends):
        logger.warning("No credential auth backends configured; falling back to local")
        from src.auth.local_auth_backend import LocalAuthBackend
        backends.append(LocalAuthBackend())
    return backends


@lru_cache
def get_auth_chain() -> PatTerminalAuthChain:
    backends = _build_backends()
    logger.info("Auth chain: %s", [b.name for b in backends])
    return PatTerminalAuthChain(backends)
