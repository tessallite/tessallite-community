"""Auth-chain configuration guards."""
from __future__ import annotations

from unittest.mock import MagicMock


def test_ldap_backend_is_skipped_when_disabled(monkeypatch):
    """A disabled LDAP backend must not be loaded just because AUTH_BACKENDS
    still contains ``ldap`` in an environment template.
    """
    from src.auth import chain

    settings = MagicMock()
    settings.AUTH_BACKENDS = "local,ldap"
    settings.LDAP_ENABLED = False

    chain.get_auth_chain.cache_clear()
    monkeypatch.setattr(chain, "get_settings", lambda: settings)

    try:
        auth_chain = chain.get_auth_chain()
        assert auth_chain.backend_names == ["pat", "local"]
    finally:
        chain.get_auth_chain.cache_clear()
