"""Bug-9225 — an SSO deployment with no public origin must fail at startup.

The community install shipped an ``.env.example`` with no ``PUBLIC_BASE_URL``,
so an operator who turned on SAML/OIDC only discovered the deployment was
unfinished when the first sign-in failed. Redirect-based login has to publish an
absolute callback address to the identity provider, and that address is read
from ``PUBLIC_BASE_URL`` — never reconstructed from a client-controlled request
header. When the variable is missing there is nothing to publish, so the process
refuses to start and names the variable.

An operator who has not enabled redirect-based SSO is unaffected.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api import sso
from src.auth.chain import REDIRECT_BACKENDS

pytestmark = pytest.mark.unit


def _settings(monkeypatch, *, auth_backends: str, public_base_url: str = ""):
    fake = SimpleNamespace(
        AUTH_BACKENDS=auth_backends,
        PUBLIC_BASE_URL=public_base_url,
    )
    monkeypatch.setattr(
        "shared.config.settings.get_settings", lambda: fake, raising=True
    )
    return fake


@pytest.mark.parametrize("backends", ["saml", "oidc", "local,saml", "local, OIDC "])
def test_sso_without_a_public_base_url_refuses_to_start(monkeypatch, backends):
    _settings(monkeypatch, auth_backends=backends, public_base_url="")

    with pytest.raises(sso.SsoConfigError) as excinfo:
        sso.sso_startup_validate()

    message = str(excinfo.value)
    # The operator must be told exactly which variable to set — this is the
    # whole point of failing here instead of at first sign-in.
    assert "PUBLIC_BASE_URL" in message
    assert "AUTH_BACKENDS" in message


def test_whitespace_only_public_base_url_is_not_a_value(monkeypatch):
    """A variable rendered from an unset template value must not read as set."""
    _settings(monkeypatch, auth_backends="saml", public_base_url="   ")

    with pytest.raises(sso.SsoConfigError):
        sso.sso_startup_validate()


def test_sso_with_a_public_base_url_starts(monkeypatch):
    _settings(
        monkeypatch,
        auth_backends="local,saml,oidc",
        public_base_url="https://tessallite.example.com",
    )

    assert sso.sso_startup_validate() is None


@pytest.mark.parametrize("backends", ["local", "", "local,ldap", "local,gcp_iam"])
def test_deployments_without_redirect_sso_are_unaffected(monkeypatch, backends):
    """The default install has no public origin configured and must still boot."""
    _settings(monkeypatch, auth_backends=backends, public_base_url="")

    assert sso.sso_startup_validate() is None


def test_the_check_uses_the_auth_chain_definition_of_redirect_backends():
    """One list, so the login chain and this guard cannot drift apart: a backend
    the chain treats as redirect-based is one this guard must cover."""
    assert REDIRECT_BACKENDS == frozenset({"saml", "oidc"})


def test_model_service_startup_calls_the_guard():
    """The guard is only worth having if the lifespan actually runs it, and it
    must not be swallowed by a defensive except the way optional startup work
    around it deliberately is."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "main.py"
    text = source.read_text(encoding="utf-8")
    assert "sso_startup_validate()" in text
    lifespan = text.split("async def lifespan", 1)[1].split("yield", 1)[0]
    assert "sso_startup_validate()" in lifespan
