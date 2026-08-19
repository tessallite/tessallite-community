"""Bug-6307 — the SSO callback origin is never taken from a client header.

``_base_url`` feeds the SAML AssertionConsumerService URL, the SP metadata
document, the ``Destination`` a SAML response is validated against, and the
OIDC ``redirect_uri``. Deriving it from ``X-Forwarded-Proto`` /
``X-Forwarded-Host`` (or the raw ``Host`` header) let an unauthenticated
caller point the IdP at a host of their choosing and capture a signed
assertion.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api import sso

pytestmark = pytest.mark.unit


class _FakeUrl:
    def __init__(self, scheme: str, netloc: str):
        self.scheme = scheme
        self.netloc = netloc


class _FakeRequest:
    def __init__(self, headers: dict | None = None, scheme="https", netloc="app.example.com"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.url = _FakeUrl(scheme, netloc)


def _settings(monkeypatch, *, public_base_url="", cors_origins=""):
    fake = SimpleNamespace(
        PUBLIC_BASE_URL=public_base_url,
        CORS_ORIGINS=cors_origins,
    )
    monkeypatch.setattr(
        "shared.config.settings.get_settings", lambda: fake, raising=True
    )
    return fake


def test_configured_public_base_url_wins_over_forwarded_headers(monkeypatch):
    _settings(monkeypatch, public_base_url="https://cloud.example.com", cors_origins="")
    req = _FakeRequest({
        "x-forwarded-proto": "https",
        "x-forwarded-host": "evil.example",
    })
    assert sso._base_url(req) == "https://cloud.example.com"


def test_configured_public_base_url_trailing_slash_is_normalised(monkeypatch):
    _settings(monkeypatch, public_base_url="https://cloud.example.com/")
    assert sso._base_url(_FakeRequest()) == "https://cloud.example.com"


def test_forwarded_host_not_in_cors_origins_is_refused(monkeypatch):
    """The core defect: an attacker-chosen host must never become the ACS URL."""
    _settings(monkeypatch, cors_origins="https://app.example.com")
    req = _FakeRequest({
        "x-forwarded-proto": "https",
        "x-forwarded-host": "evil.example",
    })
    with pytest.raises(HTTPException) as exc:
        sso._base_url(req)
    assert exc.value.status_code == 503
    assert "evil.example" not in str(exc.value.detail)


def test_raw_host_header_alone_is_refused_when_not_configured(monkeypatch):
    """No forwarded headers at all — the Host header is still client-supplied."""
    _settings(monkeypatch, cors_origins="https://app.example.com")
    req = _FakeRequest(netloc="attacker.test")
    with pytest.raises(HTTPException):
        sso._base_url(req)


def test_forwarded_host_matching_a_configured_cors_origin_is_accepted(monkeypatch):
    """Correctly proxied deployments keep working without new configuration."""
    _settings(
        monkeypatch,
        cors_origins="http://localhost:3000,https://app.example.com",
    )
    req = _FakeRequest({
        "x-forwarded-proto": "https",
        "x-forwarded-host": "app.example.com",
    })
    assert sso._base_url(req) == "https://app.example.com"


def test_scheme_downgrade_to_http_is_refused_when_only_https_is_configured(monkeypatch):
    _settings(monkeypatch, cors_origins="https://app.example.com")
    req = _FakeRequest({
        "x-forwarded-proto": "http",
        "x-forwarded-host": "app.example.com",
    })
    with pytest.raises(HTTPException):
        sso._base_url(req)


def test_appended_forwarded_values_take_the_client_facing_entry(monkeypatch):
    """A chained proxy appends; an attacker appending a second value must not
    be able to smuggle their host through as the effective origin."""
    _settings(monkeypatch, cors_origins="https://app.example.com")
    req = _FakeRequest({
        "x-forwarded-proto": "https, http",
        "x-forwarded-host": "app.example.com, evil.example",
    })
    assert sso._base_url(req) == "https://app.example.com"

    req_reversed = _FakeRequest({
        "x-forwarded-proto": "https",
        "x-forwarded-host": "evil.example, app.example.com",
    })
    with pytest.raises(HTTPException):
        sso._base_url(req_reversed)


def test_wildcard_cors_origin_is_not_an_allowlist(monkeypatch):
    """``CORS_ORIGINS=*`` must not turn into "any host may be the ACS URL"."""
    _settings(monkeypatch, cors_origins="*")
    req = _FakeRequest({"x-forwarded-host": "evil.example"})
    with pytest.raises(HTTPException):
        sso._base_url(req)


def test_embed_origins_are_not_an_allowlist(monkeypatch):
    """ALLOWED_EMBED_ORIGINS are third-party ISV sites; honouring one as our
    own callback origin would reopen the hole."""
    fake = SimpleNamespace(
        PUBLIC_BASE_URL="",
        CORS_ORIGINS="https://app.example.com",
        ALLOWED_EMBED_ORIGINS="https://isv-partner.example",
    )
    monkeypatch.setattr("shared.config.settings.get_settings", lambda: fake)
    req = _FakeRequest({"x-forwarded-host": "isv-partner.example"})
    with pytest.raises(HTTPException):
        sso._base_url(req)


def test_no_configuration_at_all_fails_closed(monkeypatch):
    _settings(monkeypatch, public_base_url="", cors_origins="")
    with pytest.raises(HTTPException):
        sso._base_url(_FakeRequest())
