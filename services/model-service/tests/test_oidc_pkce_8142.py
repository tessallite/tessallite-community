"""Bug-8142: OIDC authorization-code flow must use PKCE (RFC 7636, S256).

These tests fail against the pre-fix code, which had no PKCE at all:
``build_authorization_url`` and ``exchange_code`` did not accept a
``code_verifier`` and ``derive_code_challenge`` did not exist.

They also pin the ported-from-source TRAP: the source PKCE commit's
``exchange_code`` called ``client.post`` with NO url and NO body and swallowed
the resulting ``TypeError`` in a bare ``except Exception``, silently disabling
every OIDC login. We assert the exchange (a) actually posts to the token
endpoint with the form body AND the ``code_verifier``, (b) fails CLOSED
(returns None) on a genuine token-exchange failure, and (c) does NOT swallow a
programming error into a silent None.
"""
from __future__ import annotations

import base64
import hashlib
import types
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = pytest.mark.unit


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# derive_code_challenge — known-answer S256 vector
# ---------------------------------------------------------------------------

def test_derive_code_challenge_matches_rfc7636_appendix_b_vector():
    """RFC 7636 Appendix B fixed vector: a known verifier maps to a known S256
    challenge with the base64url padding stripped."""
    from src.auth.oidc_backend import derive_code_challenge

    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert derive_code_challenge(verifier) == expected


def test_derive_code_challenge_is_urlsafe_unpadded():
    from src.auth.oidc_backend import derive_code_challenge

    challenge = derive_code_challenge("some-verifier-value-1234567890")
    assert "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


# ---------------------------------------------------------------------------
# build_authorization_url — S256 challenge on the front channel
# ---------------------------------------------------------------------------

@pytest.fixture
def _oidc_mocks(monkeypatch):
    from src.auth import oidc_backend

    monkeypatch.setattr(
        oidc_backend, "get_oidc_config",
        lambda: {
            "issuer": "https://idp.example.com",
            "client_id": "my-client-id",
            "client_secret": "secret",
            "scopes": "openid email profile",
            "groups_claim": "groups",
        },
    )

    async def _mock_discover(issuer):
        return {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }

    monkeypatch.setattr(oidc_backend, "_discover", _mock_discover)
    return oidc_backend


@pytest.mark.asyncio
async def test_authorization_url_carries_s256_challenge(_oidc_mocks):
    verifier = "verifier-front-channel-abcdefghijklmnop-0123456789"
    url = await _oidc_mocks.build_authorization_url(
        "https://app.example.com", state="st", tenant_id="acme",
        nonce="the-nonce", code_verifier=verifier,
    )
    assert url is not None
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [_s256(verifier)]
    # The raw verifier must NEVER travel on the front channel.
    assert verifier not in url


@pytest.mark.asyncio
async def test_authorization_url_omits_pkce_when_no_verifier(_oidc_mocks):
    url = await _oidc_mocks.build_authorization_url(
        "https://app.example.com", state="st", tenant_id="acme", nonce="n",
    )
    assert url is not None
    q = parse_qs(urlparse(url).query)
    assert "code_challenge" not in q
    assert "code_challenge_method" not in q


# ---------------------------------------------------------------------------
# exchange_code — verifier replayed on the back channel; TRAP guards
# ---------------------------------------------------------------------------

def _install_token_exchange(monkeypatch, oidc_backend, post_impl):
    """Wire enough of the token-exchange + id_token path that exchange_code
    can run end to end, driving the HTTP POST through ``post_impl``."""
    async def _mock_fetch_jwks(uri):
        return {"keys": [{"kty": "RSA", "kid": "mock"}]}

    monkeypatch.setattr(oidc_backend, "_fetch_jwks", _mock_fetch_jwks)

    class MockClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            return await post_impl(url, **kwargs)

    monkeypatch.setattr(oidc_backend.httpx, "AsyncClient", MockClient)

    mock_key_set = types.SimpleNamespace()
    monkeypatch.setattr(
        oidc_backend.KeySet, "import_key_set",
        staticmethod(lambda jwks: mock_key_set),
    )
    claims = {
        "iss": "https://idp.example.com",
        "aud": "my-client-id",
        "email": "user@corp.com",
        "name": "User",
        "exp": 9999999999,
    }
    mock_token = types.SimpleNamespace(claims=claims)
    monkeypatch.setattr(
        oidc_backend.joserfc_jwt, "decode",
        lambda raw, key_set: mock_token,
    )


@pytest.mark.asyncio
async def test_exchange_code_posts_verifier_to_token_endpoint(
    _oidc_mocks, monkeypatch
):
    """The exchange must POST to the token endpoint with the authorization-code
    grant body AND the PKCE code_verifier. This is the anti-TRAP assertion: the
    source diff posted with no url and no body."""
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id_token": "jwt", "scope": "openid email profile"}

    async def _post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        return _Resp()

    _install_token_exchange(monkeypatch, _oidc_mocks, _post)

    identity = await _oidc_mocks.exchange_code(
        "https://app.example.com", "the-code", expected_nonce=None,
        code_verifier="verifier-back-channel-xyz",
    )
    assert identity is not None
    assert captured["url"] == "https://idp.example.com/token"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "the-code"
    assert captured["data"]["code_verifier"] == "verifier-back-channel-xyz"


@pytest.mark.asyncio
async def test_exchange_code_fails_closed_on_token_endpoint_error(
    _oidc_mocks, monkeypatch
):
    """A genuine token-exchange failure (non-2xx) fails CLOSED — returns None so
    the callback audits a critical failure and answers 401."""
    class _Resp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "500", request=httpx.Request("POST", "https://idp.example.com/token"),
                response=httpx.Response(500),
            )

        def json(self):  # pragma: no cover - never reached
            return {}

    async def _post(url, **kwargs):
        return _Resp()

    _install_token_exchange(monkeypatch, _oidc_mocks, _post)

    identity = await _oidc_mocks.exchange_code(
        "https://app.example.com", "the-code", code_verifier="v",
    )
    assert identity is None


@pytest.mark.asyncio
async def test_exchange_code_does_not_swallow_programming_error(
    _oidc_mocks, monkeypatch
):
    """TRAP guard: a programming error in the exchange (the source diff raised
    TypeError from a malformed post call) must NOT be swallowed into a silent
    None that masquerades as 'auth failed'. It must propagate."""
    async def _post(url, **kwargs):
        raise TypeError("post() missing url/body — the source-diff defect class")

    _install_token_exchange(monkeypatch, _oidc_mocks, _post)

    # Deliberately called WITHOUT code_verifier so the signature is valid on
    # both pre- and post-fix code: the assertion isolates the SWALLOW behaviour.
    # Pre-fix (bare ``except Exception``) swallows the TypeError into a silent
    # None, so ``pytest.raises`` fails there — exactly the defect this guards.
    with pytest.raises(TypeError):
        await _oidc_mocks.exchange_code("https://app.example.com", "the-code")
