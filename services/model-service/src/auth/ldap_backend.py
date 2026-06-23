"""LDAP auth backend — bind-and-search authentication.

Flow:
1. Bind to LDAP with service credentials (LDAP_BIND_DN / LDAP_BIND_PASSWORD).
2. Search for the user by email using the configured filter.
3. Attempt a user-credential bind with the discovered DN + supplied password.
4. If the bind succeeds, extract email, display name, and group memberships.
5. Return a ``UserIdentity`` for the auth chain.

Bug-5274: all synchronous ldap3 network calls are dispatched via
``asyncio.to_thread`` so they do not block the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.auth.backend import UserIdentity
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)

try:
    import ldap3 as _ldap3
    from ldap3 import Server as _Server, Connection as _Connection
except ImportError:
    _ldap3 = None  # type: ignore[assignment]
    _Server = None  # type: ignore[assignment,misc]
    _Connection = None  # type: ignore[assignment,misc]


def _escape(value: str) -> str:
    if _ldap3 is not None:
        return _ldap3.utils.conv.escape_filter_chars(value)
    return value


class LdapAuthBackend:
    name: str = "ldap"

    def __init__(self) -> None:
        s = get_settings()
        self._url = s.LDAP_URL
        self._bind_dn = s.LDAP_BIND_DN
        self._bind_pw = s.LDAP_BIND_PASSWORD
        self._user_base = s.LDAP_USER_SEARCH_BASE
        self._user_filter = s.LDAP_USER_SEARCH_FILTER
        self._group_base = s.LDAP_GROUP_SEARCH_BASE
        self._group_attr = s.LDAP_GROUP_ATTRIBUTE
        self._email_attr = s.LDAP_EMAIL_ATTRIBUTE
        self._display_attr = s.LDAP_DISPLAY_NAME_ATTRIBUTE
        self._use_ssl = s.LDAP_USE_SSL

    def _sync_authenticate(self, email: str, password: str) -> UserIdentity | None:
        """Run the blocking ldap3 bind-and-search sequence synchronously.

        Bug-5274: extracted so ``authenticate`` can dispatch this via
        ``asyncio.to_thread``, keeping the event loop unblocked.
        """
        server = _Server(self._url, use_ssl=self._use_ssl, get_info="ALL")

        svc_conn = _Connection(
            server, self._bind_dn, self._bind_pw,
            auto_bind=True, raise_exceptions=False,
            read_only=True,
        )
        if not svc_conn.bound:
            logger.warning("LDAP service bind failed: %s", svc_conn.result)
            return None

        search_filter = self._user_filter.replace("{email}", _escape(email))
        svc_conn.search(
            self._user_base, search_filter,
            search_scope="SUBTREE",
            attributes=[self._email_attr, self._display_attr, self._group_attr],
        )
        if not svc_conn.entries:
            svc_conn.unbind()
            return None

        entry = svc_conn.entries[0]
        user_dn = str(entry.entry_dn)
        svc_conn.unbind()

        user_conn = _Connection(
            server, user_dn, password,
            auto_bind=True, raise_exceptions=False,
        )
        if not user_conn.bound:
            return None
        user_conn.unbind()

        user_email = str(getattr(entry, self._email_attr, email))
        display_name = str(getattr(entry, self._display_attr, ""))
        groups_raw = getattr(entry, self._group_attr, [])
        groups = [str(g) for g in groups_raw] if groups_raw else []

        return UserIdentity(
            email=user_email,
            display_name=display_name,
            groups=groups,
            source_backend=self.name,
            raw_claims={"ldap_dn": user_dn},
        )

    async def authenticate(
        self, *, tenant_id: str, email: str, password: str, **kwargs: Any
    ) -> UserIdentity | None:
        if not self._url:
            return None
        if _Server is None or _Connection is None:
            logger.error("ldap3 package not installed")
            return None

        # Bug-5274: ldap3 operations are synchronous socket I/O; dispatch
        # them off the event loop so other coroutines are not starved.
        return await asyncio.to_thread(self._sync_authenticate, email, password)
