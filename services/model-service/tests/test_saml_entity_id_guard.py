"""Bug-7316: SAML IdP entityID allowlist defence-in-depth.

When SAML_ALLOWED_ENTITY_IDS is configured, _get_saml_settings must
reject metadata whose entityID is not in the list.  An empty or unset
allowlist is permissive (backwards compatible).
"""
from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from shared.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _mock_onelogin():
    """Inject a fake onelogin package so the SAML backend can import it."""
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
        # Force re-import of saml_backend so it picks up the mock
        if "src.auth.saml_backend" in sys.modules:
            importlib.reload(sys.modules["src.auth.saml_backend"])
        yield mock_parser_cls


def _cfg_with(monkeypatch, entity_ids: str = "", idp_xml: str = "<md/>"):
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "")
    monkeypatch.setenv("SAML_IDP_METADATA_XML", idp_xml)
    monkeypatch.setenv("SAML_SP_ENTITY_ID", "https://sp.example.com")
    monkeypatch.setenv("SAML_ALLOWED_ENTITY_IDS", entity_ids)
    monkeypatch.setenv("SAML_ATTR_EMAIL", "email")
    monkeypatch.setenv("SAML_ATTR_DISPLAY_NAME", "displayName")
    monkeypatch.setenv("SAML_ATTR_GROUPS", "groups")


def _fake_parse(_xml):
    return {"idp": {"entityId": "https://idp.example.com/saml/metadata"}}


def test_allowlist_rejects_untrusted_entity_id(monkeypatch, _mock_onelogin):
    _cfg_with(monkeypatch, entity_ids="https://trusted.example.com")
    _mock_onelogin.parse.side_effect = _fake_parse

    from src.auth.saml_backend import _get_saml_settings
    result = _get_saml_settings("https://app.example.com")
    assert result is None, "Untrusted entityID should have been rejected"


def test_allowlist_accepts_trusted_entity_id(monkeypatch, _mock_onelogin):
    _cfg_with(monkeypatch, entity_ids="https://idp.example.com/saml/metadata")
    _mock_onelogin.parse.side_effect = _fake_parse

    from src.auth.saml_backend import _get_saml_settings
    result = _get_saml_settings("https://app.example.com")
    assert result is not None, "Trusted entityID should have been accepted"
    assert result["idp"]["entityId"] == "https://idp.example.com/saml/metadata"


def test_empty_allowlist_is_permissive(monkeypatch, _mock_onelogin):
    _cfg_with(monkeypatch, entity_ids="")
    _mock_onelogin.parse.side_effect = _fake_parse

    from src.auth.saml_backend import _get_saml_settings
    result = _get_saml_settings("https://app.example.com")
    assert result is not None, "Empty allowlist should be permissive"


def test_allowlist_with_multiple_entries(monkeypatch, _mock_onelogin):
    _cfg_with(
        monkeypatch,
        entity_ids=(
            "https://idp.example.com/saml/metadata,"
            "https://other-idp.example.com"
        ),
    )
    _mock_onelogin.parse.side_effect = _fake_parse

    from src.auth.saml_backend import _get_saml_settings
    result = _get_saml_settings("https://app.example.com")
    assert result is not None, "EntityID in multi-entry allowlist should be accepted"
