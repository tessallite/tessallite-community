"""Bug-7778: SAML metadata-URL mode must produce a functional settings dict.

The previous code stored ``SAML_IDP_METADATA_URL`` as a ``saml_cfg["idp_metadata_url"]``
key that python3-saml's settings loader never consumes, silently producing a
non-functional backend. These tests assert KNOWN behaviour:

- a URL-configured SAML backend produces the SAME parsed ``idp`` settings dict
  as the equivalent XML-configured one (no lingering ``idp_metadata_url`` key);
- a fetch/parse failure surfaces as a clear ``None`` (unconfigured), never a
  silently-broken backend;
- the outbound metadata fetch is subject to the SSRF guard: an internal /
  non-global target is refused before any successful backend is built;
- the Bug-7316 entityID allowlist now applies to URL mode too.
"""
from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures: fake onelogin metadata parser, injected before importing the module
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from shared.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _mock_onelogin():
    onelogin_mod = types.ModuleType("onelogin")
    saml2_mod = types.ModuleType("onelogin.saml2")
    parser_mod = types.ModuleType("onelogin.saml2.idp_metadata_parser")
    mock_parser_cls = MagicMock()
    parser_mod.OneLogin_Saml2_IdPMetadataParser = mock_parser_cls
    onelogin_mod.saml2 = saml2_mod
    saml2_mod.idp_metadata_parser = parser_mod

    with patch.dict(sys.modules, {
        "onelogin": onelogin_mod,
        "onelogin.saml2": saml2_mod,
        "onelogin.saml2.idp_metadata_parser": parser_mod,
    }):
        if "src.auth.saml_backend" in sys.modules:
            importlib.reload(sys.modules["src.auth.saml_backend"])
        yield mock_parser_cls


_IDP_XML = (
    '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
    'entityID="https://idp.example.com/saml/metadata"></md:EntityDescriptor>'
)


def _parsed_idp():
    return {
        "idp": {
            "entityId": "https://idp.example.com/saml/metadata",
            "singleSignOnService": {
                "url": "https://idp.example.com/sso",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": "MIIFAKECERT",
        }
    }


def _env_url_mode(monkeypatch, url="https://idp.example.com/metadata", entity_ids=""):
    monkeypatch.setenv("SAML_IDP_METADATA_URL", url)
    monkeypatch.setenv("SAML_IDP_METADATA_XML", "")
    monkeypatch.setenv("SAML_SP_ENTITY_ID", "https://sp.example.com")
    monkeypatch.setenv("SAML_ALLOWED_ENTITY_IDS", entity_ids)
    monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "false")


def _env_xml_mode(monkeypatch, entity_ids=""):
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "")
    monkeypatch.setenv("SAML_IDP_METADATA_XML", _IDP_XML)
    monkeypatch.setenv("SAML_SP_ENTITY_ID", "https://sp.example.com")
    monkeypatch.setenv("SAML_ALLOWED_ENTITY_IDS", entity_ids)


# ---------------------------------------------------------------------------
# Core equivalence: URL mode == XML mode
# ---------------------------------------------------------------------------

def test_url_mode_produces_same_idp_dict_as_xml_mode(monkeypatch, _mock_onelogin):
    """A URL-configured backend and the equivalent XML-configured backend must
    yield identical parsed ``idp`` settings — no functional divergence."""
    _mock_onelogin.parse.return_value = _parsed_idp()
    mod = sys.modules["src.auth.saml_backend"]

    # XML mode
    _env_xml_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()
    xml_cfg = mod._get_saml_settings("https://app.example.com")

    # URL mode (fetch stubbed to return the same XML the XML branch parses)
    _env_url_mode(monkeypatch)
    get_settings.cache_clear()
    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        url_cfg = mod._get_saml_settings("https://app.example.com")

    assert xml_cfg is not None
    assert url_cfg is not None
    assert url_cfg["idp"] == xml_cfg["idp"] == _parsed_idp()["idp"]
    # Regression: the broken key must never appear.
    assert "idp_metadata_url" not in url_cfg
    assert url_cfg["sp"] == xml_cfg["sp"]


def _parsed_idp_with_sp_and_security():
    """Realistic parser output: IdP metadata that declares
    WantAuthnRequestsSigned (-> ``security`` block) and a NameIDFormat
    (-> ``sp`` block). This is what Azure AD / Okta / ADFS metadata yields."""
    d = _parsed_idp()
    d["security"] = {"authnRequestsSigned": True}
    d["sp"] = {"NameIDFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"}
    return d


def test_parsed_security_block_does_not_disable_assertion_signing(
    monkeypatch, _mock_onelogin
):
    """Bug-7778 adversarial finding: a shallow ``dict.update`` would replace the
    whole ``security`` sub-dict with the parser's (which lacks
    ``wantAssertionsSigned``), silently downgrading the SP to accept UNSIGNED
    assertions. The deep merge must preserve ``wantAssertionsSigned: True``."""
    _mock_onelogin.parse.return_value = _parsed_idp_with_sp_and_security()
    mod = sys.modules["src.auth.saml_backend"]
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        cfg = mod._get_saml_settings("https://app.example.com")

    assert cfg is not None
    # The load-bearing guarantee: signed assertions still required.
    assert cfg["security"]["wantAssertionsSigned"] is True
    assert cfg["security"]["wantNameIdEncrypted"] is False
    # Our explicit posture wins on conflicting keys: the parser's
    # authnRequestsSigned=True must NOT flip our deliberate False (we do not
    # sign AuthnRequests — no SP signing key is configured — and attacker-
    # influenced metadata must not change SP-side crypto behaviour).
    assert cfg["security"]["authnRequestsSigned"] is False


def test_metadata_cannot_disable_assertion_signing(monkeypatch, _mock_onelogin):
    """Hostile/misconfigured metadata that explicitly carries
    ``wantAssertionsSigned: False`` must NOT be able to turn off our
    assertion-signing requirement — base wins on every conflicting key."""
    hostile = _parsed_idp()
    hostile["security"] = {
        "wantAssertionsSigned": False,
        "wantNameIdEncrypted": True,
    }
    _mock_onelogin.parse.return_value = hostile
    mod = sys.modules["src.auth.saml_backend"]
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        cfg = mod._get_saml_settings("https://app.example.com")

    assert cfg is not None
    assert cfg["security"]["wantAssertionsSigned"] is True
    assert cfg["security"]["wantNameIdEncrypted"] is False


def test_parsed_sp_block_does_not_drop_entity_id_or_acs(monkeypatch, _mock_onelogin):
    """Bug-7778 adversarial finding: a shallow ``dict.update`` would replace the
    whole ``sp`` sub-dict when the metadata declares a NameIDFormat, dropping the
    SP entityId and ACS URL and yielding a configured-but-dead backend."""
    _mock_onelogin.parse.return_value = _parsed_idp_with_sp_and_security()
    mod = sys.modules["src.auth.saml_backend"]
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        cfg = mod._get_saml_settings("https://app.example.com")

    assert cfg is not None
    # SP identity and ACS survive the merge (these make SSO actually work).
    assert cfg["sp"]["entityId"] == "https://sp.example.com"
    assert cfg["sp"]["assertionConsumerService"]["url"] == (
        "https://app.example.com/api/v1/auth/saml/acs"
    )
    # A parser-suggested NameIDFormat must NOT override our explicit one
    # (base wins on conflicting keys).
    assert cfg["sp"]["NameIDFormat"] == (
        "urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress"
    )


def test_url_and_xml_modes_agree_with_full_metadata(monkeypatch, _mock_onelogin):
    """URL and XML modes must converge even when the parsed metadata carries
    sp/security blocks (the realistic case)."""
    _mock_onelogin.parse.return_value = _parsed_idp_with_sp_and_security()
    mod = sys.modules["src.auth.saml_backend"]
    from shared.config.settings import get_settings

    _env_xml_mode(monkeypatch)
    get_settings.cache_clear()
    xml_cfg = mod._get_saml_settings("https://app.example.com")

    _env_url_mode(monkeypatch)
    get_settings.cache_clear()
    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        url_cfg = mod._get_saml_settings("https://app.example.com")

    assert xml_cfg == url_cfg
    assert xml_cfg["security"]["wantAssertionsSigned"] is True
    assert xml_cfg["sp"]["entityId"] == "https://sp.example.com"


def test_url_mode_calls_parse_with_fetched_xml(monkeypatch, _mock_onelogin):
    """The fetched document is fed into the SAME ``.parse`` path as inline XML."""
    _mock_onelogin.parse.return_value = _parsed_idp()
    mod = sys.modules["src.auth.saml_backend"]
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML) as fetch:
        cfg = mod._get_saml_settings("https://app.example.com")

    fetch.assert_called_once_with("https://idp.example.com/metadata")
    _mock_onelogin.parse.assert_called_once_with(_IDP_XML)
    assert cfg is not None


# ---------------------------------------------------------------------------
# Failure modes must never yield a silently-broken backend (return None)
# ---------------------------------------------------------------------------

def test_fetch_failure_returns_none_not_broken_backend(monkeypatch, _mock_onelogin):
    mod = sys.modules["src.auth.saml_backend"]
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(
        mod, "_fetch_idp_metadata_xml",
        side_effect=mod.MetadataFetchError("boom"),
    ):
        cfg = mod._get_saml_settings("https://app.example.com")
    assert cfg is None
    _mock_onelogin.parse.assert_not_called()


def test_parse_failure_returns_none(monkeypatch, _mock_onelogin):
    mod = sys.modules["src.auth.saml_backend"]
    _mock_onelogin.parse.side_effect = ValueError("bad xml")
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value="<junk/>"):
        cfg = mod._get_saml_settings("https://app.example.com")
    assert cfg is None


def test_metadata_without_entity_id_returns_none(monkeypatch, _mock_onelogin):
    """Parsed metadata lacking an idp entityId is not a functional backend."""
    mod = sys.modules["src.auth.saml_backend"]
    _mock_onelogin.parse.return_value = {"idp": {}}
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        cfg = mod._get_saml_settings("https://app.example.com")
    assert cfg is None


def test_metadata_with_whitespace_entity_id_returns_none(monkeypatch, _mock_onelogin):
    """A whitespace-only entityId is truthy but not a usable backend; the gate
    must strip and reject it (fail closed cleanly, not build a dead backend)."""
    mod = sys.modules["src.auth.saml_backend"]
    _mock_onelogin.parse.return_value = {"idp": {"entityId": "   "}}
    _env_url_mode(monkeypatch)
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        cfg = mod._get_saml_settings("https://app.example.com")
    assert cfg is None


# ---------------------------------------------------------------------------
# entityID allowlist (Bug-7316) now applies to URL mode
# ---------------------------------------------------------------------------

def test_url_mode_allowlist_rejects_untrusted(monkeypatch, _mock_onelogin):
    mod = sys.modules["src.auth.saml_backend"]
    _mock_onelogin.parse.return_value = _parsed_idp()
    _env_url_mode(monkeypatch, entity_ids="https://trusted.example.com")
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        cfg = mod._get_saml_settings("https://app.example.com")
    assert cfg is None, "untrusted entityID must be rejected in URL mode"


def test_url_mode_allowlist_accepts_trusted(monkeypatch, _mock_onelogin):
    mod = sys.modules["src.auth.saml_backend"]
    _mock_onelogin.parse.return_value = _parsed_idp()
    _env_url_mode(
        monkeypatch, entity_ids="https://idp.example.com/saml/metadata",
    )
    from shared.config.settings import get_settings
    get_settings.cache_clear()

    with patch.object(mod, "_fetch_idp_metadata_xml", return_value=_IDP_XML):
        cfg = mod._get_saml_settings("https://app.example.com")
    assert cfg is not None
    assert cfg["idp"]["entityId"] == "https://idp.example.com/saml/metadata"


# ---------------------------------------------------------------------------
# SSRF guard on the outbound fetch
# ---------------------------------------------------------------------------

class TestMetadataFetchSSRF:
    """_fetch_idp_metadata_xml must refuse internal / non-global targets and
    disallowed schemes before any successful backend is built."""

    def _mod(self):
        import src.auth.saml_backend as mod
        return mod

    def test_preflight_rejects_localhost(self):
        mod = self._mod()
        with pytest.raises(mod.MetadataFetchError):
            mod._fetch_idp_metadata_xml("https://localhost/metadata")

    def test_preflight_rejects_cloud_metadata_host(self):
        mod = self._mod()
        with pytest.raises(mod.MetadataFetchError):
            mod._fetch_idp_metadata_xml(
                "http://metadata.google.internal/computeMetadata/v1/"
            )

    def test_preflight_rejects_literal_private_ip(self):
        mod = self._mod()
        with pytest.raises(mod.MetadataFetchError):
            mod._fetch_idp_metadata_xml("https://169.254.169.254/latest/")

    def test_preflight_rejects_http_when_not_allowed(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "false")
        from shared.config.settings import get_settings
        get_settings.cache_clear()
        mod = self._mod()
        with pytest.raises(mod.MetadataFetchError):
            mod._fetch_idp_metadata_xml("http://idp.example.com/metadata")

    def test_connect_time_backend_blocks_rebinding_to_private_ip(self):
        """A public hostname that resolves to a private address at connect time
        must be blocked by the pinning backend (DNS-rebinding defence)."""
        import httpcore
        mod = self._mod()
        backend = mod._SSRFSafeSyncBackend()
        # 127.0.0.1 is non-global; getaddrinfo returns it for a "public" name.
        fake_infos = [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]
        with patch("socket.getaddrinfo", return_value=fake_infos):
            with pytest.raises(httpcore.ConnectError):
                backend.connect_tcp("evil.example.com", 443)

    def test_connect_time_backend_allows_global_ip(self):
        mod = self._mod()
        backend = mod._SSRFSafeSyncBackend()
        fake_infos = [(2, 1, 6, "", ("93.184.216.34", 443))]  # example.com, global
        sentinel = object()
        backend._inner = MagicMock()
        backend._inner.connect_tcp.return_value = sentinel
        with patch("socket.getaddrinfo", return_value=fake_infos):
            result = backend.connect_tcp("idp.example.com", 443)
        assert result is sentinel
        # Connection must target the validated IP, not the hostname.
        assert backend._inner.connect_tcp.call_args.args[0] == "93.184.216.34"

    def test_transport_asserts_pool_assignment(self):
        """The SSRF-safe transport must actually install the guarded pool."""
        mod = self._mod()
        transport = mod._ssrf_safe_sync_transport()
        assert transport._pool is not None
        assert transport._pool._network_backend.__class__.__name__ == (
            "_SSRFSafeSyncBackend"
        )


# ---------------------------------------------------------------------------
# Bug-7855: fetched IdP metadata is cached (LRU + TTL) so the unauthenticated
# /saml/metadata and /saml/login routes do not drive one outbound GET each.
# ---------------------------------------------------------------------------

class TestMetadataFetchCache:
    def _mod(self):
        import src.auth.saml_backend as mod
        return mod

    def _fake_client(self, xml_bytes: bytes, counter: list):
        class _Resp:
            status_code = 200

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def iter_bytes(self_inner):
                yield xml_bytes

        class _Client:
            def __init__(self_inner, **kwargs):
                pass

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def stream(self_inner, method, url, **kwargs):
                counter.append(url)
                return _Resp()

        return _Client

    def test_second_fetch_within_ttl_served_from_cache(self, monkeypatch):
        """A repeated fetch of the same URL within the TTL must NOT drive a
        second outbound HTTP round-trip (Bug-7855)."""
        mod = self._mod()
        mod._reset_metadata_cache()
        counter: list = []
        monkeypatch.setattr(mod, "validate_webhook_url", lambda u: u)
        monkeypatch.setattr(mod, "_ssrf_safe_sync_transport", lambda: object())
        monkeypatch.setattr(
            mod.httpx, "Client", self._fake_client(b"<md/>", counter)
        )

        url = "https://idp.example.com/metadata"
        try:
            first = mod._fetch_idp_metadata_xml(url)
            second = mod._fetch_idp_metadata_xml(url)
            assert first == second == "<md/>"
            assert len(counter) == 1, "expected exactly one outbound fetch"
        finally:
            mod._reset_metadata_cache()

    def test_ttl_expiry_triggers_refetch(self, monkeypatch):
        """Once the TTL elapses the cached document is dropped and the next
        call fetches again — staleness is bounded, not permanent."""
        mod = self._mod()
        mod._reset_metadata_cache()
        counter: list = []
        monkeypatch.setattr(mod, "validate_webhook_url", lambda u: u)
        monkeypatch.setattr(mod, "_ssrf_safe_sync_transport", lambda: object())
        monkeypatch.setattr(
            mod.httpx, "Client", self._fake_client(b"<md/>", counter)
        )
        clock = {"t": 1000.0}
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

        url = "https://idp.example.com/metadata"
        try:
            mod._fetch_idp_metadata_xml(url)
            clock["t"] += mod._METADATA_CACHE_TTL_SECONDS + 1
            mod._fetch_idp_metadata_xml(url)
            assert len(counter) == 2, "expired cache entry must be re-fetched"
        finally:
            mod._reset_metadata_cache()

    def test_distinct_urls_are_cached_independently(self, monkeypatch):
        mod = self._mod()
        mod._reset_metadata_cache()
        counter: list = []
        monkeypatch.setattr(mod, "validate_webhook_url", lambda u: u)
        monkeypatch.setattr(mod, "_ssrf_safe_sync_transport", lambda: object())
        monkeypatch.setattr(
            mod.httpx, "Client", self._fake_client(b"<md/>", counter)
        )
        try:
            mod._fetch_idp_metadata_xml("https://idp-a.example.com/metadata")
            mod._fetch_idp_metadata_xml("https://idp-b.example.com/metadata")
            # Two distinct URLs -> two fetches; neither served the other.
            assert len(counter) == 2
        finally:
            mod._reset_metadata_cache()
