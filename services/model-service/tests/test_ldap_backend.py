"""Unit tests for the LDAP auth backend using mocked ldap3."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest


@pytest.fixture
def ldap_settings(monkeypatch):
    monkeypatch.setenv("LDAP_URL", "ldap://test.example.com")
    monkeypatch.setenv("LDAP_BIND_DN", "cn=svc,dc=test")
    monkeypatch.setenv("LDAP_BIND_PASSWORD", "svc-pw")
    monkeypatch.setenv("LDAP_USER_SEARCH_BASE", "ou=users,dc=test")
    monkeypatch.setenv("LDAP_USER_SEARCH_FILTER", "(mail={email})")
    monkeypatch.setenv("LDAP_GROUP_SEARCH_BASE", "ou=groups,dc=test")
    monkeypatch.setenv("LDAP_USE_SSL", "false")
    from shared.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_entry(dn: str, email: str, display: str, groups: list[str]):
    entry = MagicMock()
    entry.entry_dn = dn
    entry.mail = email
    entry.displayName = display
    entry.memberOf = groups
    return entry


@pytest.mark.asyncio
async def test_ldap_authenticate_success(ldap_settings):
    mock_entry = _make_entry(
        "cn=alice,ou=users,dc=test", "alice@test.com", "Alice",
        ["cn=admins,ou=groups,dc=test"],
    )

    svc_conn = MagicMock()
    svc_conn.bound = True
    svc_conn.entries = [mock_entry]

    user_conn = MagicMock()
    user_conn.bound = True

    with patch("src.auth.ldap_backend._Server") as MockServer, \
         patch("src.auth.ldap_backend._Connection") as MockConn, \
         patch("src.auth.ldap_backend._escape", side_effect=lambda x: x):
        MockConn.side_effect = [svc_conn, user_conn]

        from src.auth.ldap_backend import LdapAuthBackend
        backend = LdapAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="alice@test.com", password="secret"
        )

    assert identity is not None
    assert identity.email == "alice@test.com"
    assert identity.display_name == "Alice"
    assert identity.source_backend == "ldap"
    assert "ldap_dn" in identity.raw_claims


@pytest.mark.asyncio
async def test_ldap_authenticate_bad_password(ldap_settings):
    mock_entry = _make_entry("cn=alice,ou=users,dc=test", "alice@test.com", "Alice", [])

    svc_conn = MagicMock()
    svc_conn.bound = True
    svc_conn.entries = [mock_entry]

    user_conn = MagicMock()
    user_conn.bound = False

    with patch("src.auth.ldap_backend._Server"), \
         patch("src.auth.ldap_backend._Connection") as MockConn, \
         patch("src.auth.ldap_backend._escape", side_effect=lambda x: x):
        MockConn.side_effect = [svc_conn, user_conn]

        from src.auth.ldap_backend import LdapAuthBackend
        backend = LdapAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="alice@test.com", password="wrong"
        )

    assert identity is None


@pytest.mark.asyncio
async def test_ldap_authenticate_user_not_found(ldap_settings):
    svc_conn = MagicMock()
    svc_conn.bound = True
    svc_conn.entries = []

    with patch("src.auth.ldap_backend._Server"), \
         patch("src.auth.ldap_backend._Connection") as MockConn, \
         patch("src.auth.ldap_backend._escape", side_effect=lambda x: x):
        MockConn.return_value = svc_conn

        from src.auth.ldap_backend import LdapAuthBackend
        backend = LdapAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="nobody@test.com", password="pw"
        )

    assert identity is None


@pytest.mark.asyncio
async def test_ldap_service_bind_failure(ldap_settings):
    svc_conn = MagicMock()
    svc_conn.bound = False
    svc_conn.result = {"description": "invalidCredentials"}

    with patch("src.auth.ldap_backend._Server"), \
         patch("src.auth.ldap_backend._Connection") as MockConn, \
         patch("src.auth.ldap_backend._escape", side_effect=lambda x: x):
        MockConn.return_value = svc_conn

        from src.auth.ldap_backend import LdapAuthBackend
        backend = LdapAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="alice@test.com", password="pw"
        )

    assert identity is None


@pytest.mark.asyncio
async def test_ldap_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setenv("LDAP_URL", "")
    from shared.config.settings import get_settings
    get_settings.cache_clear()
    try:
        from src.auth.ldap_backend import LdapAuthBackend
        backend = LdapAuthBackend()
        identity = await backend.authenticate(
            tenant_id="t1", email="a@b.com", password="pw"
        )
        assert identity is None
    finally:
        get_settings.cache_clear()
